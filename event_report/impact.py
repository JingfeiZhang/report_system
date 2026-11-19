"""
GET /api/v1/event-report/impact

事件详情「影响评估与舆情分析」。
优先级：mock → 8250 display + 达梦词云/观点 → 短时 LLM 叙事；
无库且 ``EVENT_REPORT_DEMO_MODE=true`` 时灌满演示样例。
契约对齐：事件详情_影响评估与舆情分析接口定义.md
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from loguru import logger

from event_report.dm_event_stats import (
    demo_mode_enabled,
    dm_enabled,
    fetch_top_posts,
    fetch_word_cloud,
)
from event_report.impact_llm import fill_impact_narrative, llm_enabled
from event_report.mock_catalog import MOCK_IMPACT, build_demo_impact
from event_report.upstream_event_display import fetch_event_display
from event_report.upstream_open_events import open_events_source

router = APIRouter(tags=["EventReport"])

_TZ_CN = timezone(timedelta(hours=8))
_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _now_iso() -> str:
    return datetime.now(_TZ_CN).replace(microsecond=0).isoformat()


def _cache_ttl() -> float:
    try:
        return max(0.0, float(__import__("os").getenv("EVENT_REPORT_CACHE_TTL_SEC") or "300"))
    except ValueError:
        return 300.0


def _empty_media_opinions() -> Dict[str, List[Dict[str, Any]]]:
    return {"domestic": [], "international": []}


def _skeleton(main_event_id: Union[int, str]) -> Dict[str, Any]:
    """未知 ID 也返回完整骨架，避免前端字段缺失崩溃。"""
    return {
        "main_event_id": main_event_id,
        "impact_scope": "暂无",
        "affected_population": "暂无量化数据",
        "casualties": "暂无权威伤亡数据",
        "economic_loss": "暂无量化数据",
        "resolution_result": "暂无后续处理信息",
        "avg_goldstein": 0.0,
        "avg_tone": 0.0,
        "word_cloud": [],
        "media_opinions": _empty_media_opinions(),
        "updated_at": _now_iso(),
    }


def _is_impact_sparse(data: Dict[str, Any]) -> bool:
    wc = data.get("word_cloud") or []
    mo = data.get("media_opinions") or {}
    domestic = mo.get("domestic") or [] if isinstance(mo, dict) else []
    international = mo.get("international") or [] if isinstance(mo, dict) else []
    scope = str(data.get("impact_scope") or "").strip()
    if wc or domestic or international:
        return False
    return scope in ("", "暂无")


def _demo_or_skeleton_impact(
    main_event_id: Union[int, str],
    *,
    title: str = "",
    event_date: str = "2026-08-06",
) -> Dict[str, Any]:
    if demo_mode_enabled():
        data = build_demo_impact(
            main_event_id, title=title, event_date=event_date
        )
        data["updated_at"] = _now_iso()
        return data
    data = _skeleton(main_event_id)
    data["source"] = "skeleton"
    return data


def _overlay_demo_narrative(data: Dict[str, Any], *, title: str, event_date: str, mid: Union[int, str]) -> Dict[str, Any]:
    """保留演示表词云/观点，空叙事字段用模板补齐。"""
    demo = build_demo_impact(mid, title=title, event_date=event_date)
    out = dict(data)
    for key in (
        "impact_scope",
        "affected_population",
        "casualties",
        "economic_loss",
        "resolution_result",
    ):
        cur = str(out.get(key) or "").strip()
        if cur in ("", "暂无", "暂无量化数据", "暂无权威伤亡数据", "暂无后续处理信息"):
            out[key] = demo.get(key)
    if not out.get("word_cloud"):
        out["word_cloud"] = demo.get("word_cloud") or []
    mo = out.get("media_opinions") or {}
    if not (mo.get("domestic") or mo.get("international")):
        out["media_opinions"] = demo.get("media_opinions")
    if abs(float(out.get("avg_goldstein") or 0.0)) < 1e-9 and abs(float(out.get("avg_tone") or 0.0)) < 1e-9:
        out["avg_goldstein"] = demo.get("avg_goldstein")
        out["avg_tone"] = demo.get("avg_tone")
    out["source"] = "demo_dm"
    out["main_event_id"] = mid
    out["updated_at"] = _now_iso()
    return out


def _posts_to_media_opinions(posts: List[Any]) -> Dict[str, List[Dict[str, Any]]]:
    domestic: List[Dict[str, Any]] = []
    for p in posts[:5]:
        view = (getattr(p, "content", None) or "").strip()
        if len(view) > 60:
            view = view[:57] + "…"
        if len(view) < 6:
            continue
        heat = int(getattr(p, "heat", 0) or 0)
        score = max(1, min(100, heat // 10 if heat else 40))
        domestic.append(
            {
                "source": getattr(p, "account_name", None)
                or getattr(p, "platform", None)
                or "自媒体",
                "view": view,
                "sentiment_score": score,
                "sentiment_label": "关注",
                "url": getattr(p, "url", None),
                "published_at": getattr(p, "published_at", None),
            }
        )
    return {"domestic": domestic, "international": []}


def _merge_llm(
    base: Dict[str, Any],
    llm: Dict[str, Any],
) -> Dict[str, Any]:
    out = dict(base)
    for key in (
        "impact_scope",
        "affected_population",
        "casualties",
        "economic_loss",
        "resolution_result",
    ):
        val = llm.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    for key in ("avg_goldstein", "avg_tone"):
        try:
            out[key] = float(llm.get(key))
        except (TypeError, ValueError):
            pass
    mo = llm.get("media_opinions")
    if isinstance(mo, dict):
        domestic = mo.get("domestic")
        international = mo.get("international")
        merged = dict(out.get("media_opinions") or _empty_media_opinions())
        if isinstance(domestic, list) and domestic:
            merged["domestic"] = domestic[:5]
        if isinstance(international, list) and international:
            merged["international"] = international[:5]
        out["media_opinions"] = merged
    return out


# 兼容旧名
_MOCK_BY_ID: Dict[str, Dict[str, Any]] = MOCK_IMPACT


def build_impact_data(main_event_id: Union[int, str]) -> Dict[str, Any]:
    """仅 mock / 骨架（同步，供单测）。"""
    key = str(main_event_id).strip()
    if not key:
        raise ValueError("main_event_id 不能为空")

    cached = MOCK_IMPACT.get(key)
    if cached:
        data = dict(cached)
        try:
            data["main_event_id"] = int(key)
        except ValueError:
            data["main_event_id"] = key
        data["updated_at"] = _now_iso()
        return data

    try:
        mid: Union[int, str] = int(key)
    except ValueError:
        mid = key
    return _demo_or_skeleton_impact(mid)


async def resolve_impact_data(main_event_id: Union[int, str]) -> Dict[str, Any]:
    """mock → display+DM → 可选 LLM；空数据且演示模式则灌满。"""
    key = str(main_event_id).strip()
    if not key:
        raise ValueError("main_event_id 不能为空")

    ttl = _cache_ttl()
    cache_key = f"impact:{key}"
    if ttl > 0 and cache_key in _CACHE:
        ts, payload = _CACHE[cache_key]
        if time.time() - ts < ttl:
            out = dict(payload)
            out["updated_at"] = _now_iso()
            return out

    # 1) mock
    mock = MOCK_IMPACT.get(key)
    if mock:
        data = dict(mock)
        try:
            data["main_event_id"] = int(key)
        except ValueError:
            data["main_event_id"] = key
        data["updated_at"] = _now_iso()
        if ttl > 0:
            _CACHE[cache_key] = (time.time(), dict(data))
        return data

    try:
        mid: Union[int, str] = int(key)
    except ValueError:
        mid = key

    data = _skeleton(mid)

    if open_events_source() == "mock":
        data = _demo_or_skeleton_impact(mid)
        if ttl > 0:
            _CACHE[cache_key] = (time.time(), dict(data))
        return data

    title = ""
    summary = ""
    event_date = "2026-08-06"
    try:
        display = await fetch_event_display(mid, event_type="monitor")
        title = str(display.get("title") or "").strip()
        summary = str(display.get("summary") or "").strip()
        ed = str(display.get("event_date") or "").strip()[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", ed):
            event_date = ed
        # display 可能已有部分指数
        for k in ("avg_goldstein", "avg_tone"):
            if display.get(k) is not None:
                try:
                    data[k] = float(display[k])
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.warning("impact 上游 display 失败 id={} err={!r}", mid, e)

    posts: List[Any] = []
    post_dicts: List[Dict[str, Any]] = []
    if dm_enabled() and title:
        try:
            data["word_cloud"] = fetch_word_cloud(
                title, event_date, before=5, after=3, limit=20
            )
            posts = fetch_top_posts(title, event_date, before=5, after=3, limit=10)
            if posts:
                data["media_opinions"] = _posts_to_media_opinions(posts)
            post_dicts = [
                {
                    "platform": p.platform,
                    "account_name": p.account_name,
                    "content": p.content,
                    "url": p.url,
                    "published_at": p.published_at,
                }
                for p in posts
            ]
        except Exception as e:
            logger.warning("impact 达梦聚合失败 id={} err={!r}", mid, e)
            posts = []
            post_dicts = []

    # 结构化仍空：演示优先灌满，避免 LLM 超时拖垮演示
    if _is_impact_sparse(data) and demo_mode_enabled():
        data = _demo_or_skeleton_impact(mid, title=title, event_date=event_date)
        if ttl > 0:
            _CACHE[cache_key] = (time.time(), dict(data))
        return data

    # 演示模式已有评论表数据：跳过 LLM，叙事空位用模板补齐
    if demo_mode_enabled() and not _is_impact_sparse(data):
        data = _overlay_demo_narrative(
            data, title=title, event_date=event_date, mid=mid
        )
        if ttl > 0:
            _CACHE[cache_key] = (time.time(), dict(data))
        return data

    if llm_enabled() and title:
        llm = fill_impact_narrative(
            title=title,
            summary=summary,
            event_date=event_date,
            posts=post_dicts,
        )
        if llm:
            data = _merge_llm(data, llm)

    if _is_impact_sparse(data) and demo_mode_enabled():
        data = _demo_or_skeleton_impact(mid, title=title, event_date=event_date)
    else:
        data["main_event_id"] = mid
        data["updated_at"] = _now_iso()
        if "source" not in data or not data.get("source"):
            data["source"] = "dm_llm" if (title and dm_enabled()) else "skeleton"

    if ttl > 0:
        _CACHE[cache_key] = (time.time(), dict(data))
    return data


def _ok(data: Any, msg: str = "success", code: int = 0) -> JSONResponse:
    return JSONResponse(content={"code": code, "msg": msg, "data": data})


@router.get("/api/v1/event-report/impact")
async def get_event_report_impact(
    main_event_id: str = Query(..., description="主事件 ID"),
):
    """
    影响评估与舆情分析（详情页）。

    mock 窗口 ID 仍返回样例；其它 ID：display + 达梦 + 短时 LLM；
    演示模式下无库数据灌满样例。
    """
    try:
        data = await resolve_impact_data(main_event_id)
    except ValueError as e:
        return _ok(data=None, msg=str(e), code=400)
    return _ok(data)
