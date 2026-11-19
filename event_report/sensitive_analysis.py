"""
GET /api/v1/main-events/{id}/sensitive-analysis

左屏敏感事件展开详情。

数据优先级：
1. 本地 mock_catalog（旧演示 ID）
2. 上游 8250 ``event-report/display`` 取 title/event_date + 达梦聚合热度/趋势/观点
3. 达梦无数据时：``EVENT_REPORT_DEMO_MODE=true`` 灌满演示样例；否则空骨架 /
   可用 display 的 total_mentions/gdelt_count 作非零兜底

契约：
- 7-31新需求/左屏敏感事件展开详情接口定义.md
- 7-31新需求/厂家敏感分析接口改造对接说明.md（方案 A：Query event_type）
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse
from loguru import logger

from event_report.dm_event_stats import (
    demo_mode_enabled,
    dm_enabled,
    fetch_daily_heat,
    fetch_same_period_heat,
    fetch_top_posts,
    heat_trend_from_daily,
)
from event_report.mock_catalog import (
    MOCK_SENSITIVE_MONITOR,
    MOCK_SENSITIVE_RECORD,
    build_demo_sensitive,
)
from event_report.upstream_event_display import fetch_event_display
from event_report.upstream_open_events import open_events_source

router = APIRouter(tags=["SensitiveAnalysis"])

_TZ_CN = timezone(timedelta(hours=8))
_VALID_EVENT_TYPES = frozenset({"monitor", "record"})


def _now_iso() -> str:
    return datetime.now(_TZ_CN).replace(microsecond=0).isoformat()


def _parse_event_id(raw: Union[int, str]) -> Union[int, str]:
    key = str(raw).strip()
    if not key:
        raise ValueError("事件 id 不能为空")
    try:
        return int(key)
    except ValueError:
        return key


def _normalize_event_type(event_type: Optional[str]) -> str:
    """缺省按 monitor（兼容旧调用）。"""
    if event_type is None or str(event_type).strip() == "":
        return "monitor"
    et = str(event_type).strip().lower()
    if et not in _VALID_EVENT_TYPES:
        raise ValueError("event_type 须为 monitor 或 record")
    return et


def _attach_identity(
    data: Dict[str, Any],
    *,
    event_type: str,
    event_id: Union[int, str],
) -> Dict[str, Any]:
    out = dict(data)
    out["event_type"] = event_type
    out["updated_at"] = _now_iso()
    if event_type == "record":
        out["main_event_id"] = None
        out["record_event_id"] = event_id
    else:
        out["main_event_id"] = event_id
        out["record_event_id"] = None
    return out


def _empty_skeleton(
    event_id: Union[int, str],
    *,
    event_type: str = "monitor",
    event_date: str = "2026-08-06",
) -> Dict[str, Any]:
    """无分析数据时仍 code=0，返回空骨架；ID / event_type 必须正确回显。"""
    try:
        event_dt = datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        event_date = "2026-08-06"
        event_dt = datetime.strptime(event_date, "%Y-%m-%d")
    start = (event_dt - timedelta(days=5)).strftime("%Y-%m-%d")
    end = (event_dt + timedelta(days=3)).strftime("%Y-%m-%d")
    base = {
        "event_date": event_date,
        "heat": {
            "total": 0,
            "comparison_base": "previous_day",
            "previous_value": None,
            "change_rate": None,
            "change_direction": "unknown",
        },
        "same_period_comparison": [],
        "observation_window": {
            "before_days": 5,
            "after_days": 3,
            "start_date": start,
            "event_date": event_date,
            "end_date": end,
            "status": "active",
        },
        "heat_trend": [],
        "self_media_opinions": [],
    }
    return _attach_identity(base, event_type=event_type, event_id=event_id)


def _is_sensitive_sparse(data: Dict[str, Any]) -> bool:
    """热度全空且无观点 → 演示模式下应灌满。"""
    source = str(data.get("source") or "")
    if source in ("empty", "dm_disabled"):
        return True
    opinions = data.get("self_media_opinions") or []
    heat_total = int((data.get("heat") or {}).get("total") or 0)
    trend = data.get("heat_trend") or []
    trend_heat = sum(int(p.get("heat") or 0) for p in trend if isinstance(p, dict))
    if opinions:
        return False
    return heat_total <= 0 and trend_heat <= 0


def _demo_or_skeleton(
    event_id: Union[int, str],
    *,
    event_type: str,
    title: str = "",
    event_date: str = "2026-08-06",
) -> Dict[str, Any]:
    if demo_mode_enabled():
        demo = build_demo_sensitive(
            event_id,
            event_type=event_type,
            title=title,
            event_date=event_date,
        )
        return _attach_identity(demo, event_type=event_type, event_id=event_id)
    return _empty_skeleton(event_id, event_type=event_type, event_date=event_date)


def _peak_from_display(display: Dict[str, Any], event_id: Union[int, str]) -> int:
    for key in ("total_mentions", "gdelt_count"):
        val = display.get(key)
        try:
            n = int(val)
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    return 0


def _opinions_from_posts(posts: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(posts[:3], 1):
        content = (getattr(p, "content", None) or "").strip()
        if len(content) > 80:
            content = content[:77] + "…"
        if len(content) < 8:
            continue
        published = getattr(p, "published_at", None) or _now_iso()
        url = getattr(p, "url", None) or f"https://example.com/post/{10000 + i}"
        out.append(
            {
                "rank": i,
                "account_id": f"media_{10000 + i}",
                "account_name": getattr(p, "account_name", None) or "未知账号",
                "platform": getattr(p, "platform", None) or "自媒体",
                "content": content,
                "interaction_heat": int(getattr(p, "heat", 0) or 0),
                "published_at": published,
                "source_url": url,
            }
        )
    return out


def _build_from_display_and_dm(
    display: Dict[str, Any],
    *,
    event_type: str,
    event_id: Union[int, str],
) -> Dict[str, Any]:
    """display 元数据 + 达梦真趋势；DM 空时演示灌满或 display 计数兜底。"""
    event_date = str(display.get("event_date") or "").strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", event_date):
        event_date = "2026-08-06"

    title = str(display.get("title") or f"事件{event_id}").strip()
    before, after = 5, 3

    daily = fetch_daily_heat(title, event_date, before=before, after=after)
    heat_by_date = dict(daily.heat_by_date or {})
    source = "demo_dm" if demo_mode_enabled() else "dm"

    # DM 无帖：事件日用 display 计数兜底，其余日保持 0（不伪造曲线）
    if daily.total_posts <= 0:
        peak = _peak_from_display(display, event_id)
        if peak > 0 and not demo_mode_enabled():
            heat_by_date[event_date] = peak
            source = "upstream_display_fallback"
        else:
            source = "empty"

    if source == "empty" and demo_mode_enabled():
        return _demo_or_skeleton(
            event_id, event_type=event_type, title=title, event_date=event_date
        )

    trend, window, heat = heat_trend_from_daily(
        event_date, heat_by_date, before=before, after=after
    )

    same_period = fetch_same_period_heat(title, event_date, years_back=3)
    # 无历史数据时仍返回年份结构但 heat=0；若 DM 不可用则空数组
    if daily.error == "dm_unavailable" and not same_period:
        same_period = []

    posts = fetch_top_posts(title, event_date, before=before, after=after, limit=3)
    opinions = _opinions_from_posts(posts)

    base = {
        "event_date": event_date,
        "heat": heat,
        "same_period_comparison": same_period,
        "observation_window": window,
        "heat_trend": trend,
        "self_media_opinions": opinions,
        "title": title,
        "source": source,
    }
    out = _attach_identity(base, event_type=event_type, event_id=event_id)
    if _is_sensitive_sparse(out) and demo_mode_enabled():
        return _demo_or_skeleton(
            event_id, event_type=event_type, title=title, event_date=event_date
        )
    return out


# 兼容旧模块内引用名
_MOCK_BY_MONITOR_ID = MOCK_SENSITIVE_MONITOR
_MOCK_BY_RECORD_ID = MOCK_SENSITIVE_RECORD


def build_sensitive_analysis_data(
    event_id: Union[int, str],
    *,
    event_type: Optional[str] = None,
) -> Dict[str, Any]:
    """仅查本地 mock（同步，供单测）。未命中则空骨架 / 演示灌满。"""
    et = _normalize_event_type(event_type)
    eid = _parse_event_id(event_id)
    key = str(eid)

    store = MOCK_SENSITIVE_RECORD if et == "record" else MOCK_SENSITIVE_MONITOR
    cached = store.get(key)
    if cached:
        return _attach_identity(cached, event_type=et, event_id=eid)

    default_date = "2022-10-13" if et == "record" else "2026-08-06"
    return _demo_or_skeleton(eid, event_type=et, event_date=default_date)


async def resolve_sensitive_analysis_data(
    event_id: Union[int, str],
    *,
    event_type: Optional[str] = None,
) -> Dict[str, Any]:
    """mock 优先；未命中且非纯 mock 模式时拉 display + 达梦聚合。"""
    et = _normalize_event_type(event_type)
    eid = _parse_event_id(event_id)
    key = str(eid)

    store = MOCK_SENSITIVE_RECORD if et == "record" else MOCK_SENSITIVE_MONITOR
    cached = store.get(key)
    if cached:
        return _attach_identity(cached, event_type=et, event_id=eid)

    if open_events_source() == "mock":
        default_date = "2022-10-13" if et == "record" else "2026-08-06"
        return _demo_or_skeleton(eid, event_type=et, event_date=default_date)

    try:
        display = await fetch_event_display(eid, event_type=et)
        if dm_enabled():
            return _build_from_display_and_dm(display, event_type=et, event_id=eid)
        # DM 关闭：演示模式灌满，否则仅元数据 + 空热度骨架
        event_date = str(display.get("event_date") or "").strip()[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", event_date):
            event_date = "2026-08-06"
        title = str(display.get("title") or "").strip()
        if demo_mode_enabled():
            return _demo_or_skeleton(
                eid, event_type=et, title=title, event_date=event_date
            )
        data = _empty_skeleton(eid, event_type=et, event_date=event_date)
        data["title"] = title
        data["source"] = "dm_disabled"
        return data
    except Exception as e:
        logger.warning(
            "sensitive-analysis 上游 display/DM 失败 id={} type={} err={!r}",
            eid,
            et,
            e,
        )
        default_date = "2022-10-13" if et == "record" else "2026-08-06"
        return _demo_or_skeleton(eid, event_type=et, event_date=default_date)


def _ok(data: Any, msg: str = "success", code: int = 0) -> JSONResponse:
    return JSONResponse(content={"code": code, "msg": msg, "data": data})


@router.get("/api/v1/main-events/{event_id}/sensitive-analysis")
async def get_sensitive_analysis(
    event_id: str = Path(..., description="事件 ID；含义由 event_type 决定"),
    event_type: Optional[str] = Query(
        None,
        description="monitor | record；缺省按 monitor（兼容旧调用）",
    ),
):
    """
    敏感事件展开详情。

    - ``event_type=monitor``（或不传）：路径 id = main_event_id
    - ``event_type=record``：路径 id = record_event_id
    - 本地 mock 未覆盖时：8250 display + 达梦热度/观点
    """
    try:
        data = await resolve_sensitive_analysis_data(event_id, event_type=event_type)
    except ValueError as e:
        return JSONResponse(
            status_code=422,
            content={
                "code": 42201,
                "msg": str(e) or "参数无效",
                "detail": str(e),
                "data": None,
            },
        )
    return _ok(data)
