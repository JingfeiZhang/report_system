"""
GET /api/v1/open/events

对外开放事件列表。

默认（方案 A）：代理平台 8250 同名接口，事件 ID 以上游为准。
可选：OPEN_EVENTS_SOURCE=mock 走本地 mock_catalog（单测 / 离线）。

配置：
  OPEN_EVENTS_SOURCE=upstream|mock          # 默认 upstream
  OPEN_EVENTS_UPSTREAM_BASE_URL=http://192.168.1.157:8250
  OPEN_EVENTS_UPSTREAM_TIMEOUT_SEC=15
  OPEN_EVENTS_FALLBACK_MOCK=false           # upstream 失败时是否回退 mock
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from loguru import logger

from event_report.mock_catalog import (
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    MOCK_LIST_ITEMS,
)
from event_report.upstream_open_events import (
    fallback_mock_enabled,
    fetch_open_events_from_upstream,
    open_events_source,
    upstream_base_url,
)

router = APIRouter(tags=["OpenEvents"])

_ALL_MAX = 1000

# 兼容旧测试 / 外部引用
_MOCK_ITEMS: List[Dict[str, Any]] = MOCK_LIST_ITEMS


def _ok(data: Any, msg: str = "success", code: int = 0) -> JSONResponse:
    return JSONResponse(content={"code": code, "msg": msg, "data": data})


def _parse_date(value: str, field: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"{field} 格式应为 YYYY-MM-DD") from e


def _validate_date_pair(start_date: Optional[str], end_date: Optional[str]) -> None:
    if (start_date is None) ^ (end_date is None):
        raise HTTPException(
            status_code=400,
            detail="start_date 与 end_date 需同时传或同时省略",
        )
    if start_date is None:
        return
    start_dt = _parse_date(start_date, "start_date")
    end_dt = _parse_date(end_date, "end_date")
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")


def filter_open_events(
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    topic_id: Optional[str] = None,
    region: Optional[str] = None,
    keyword: Optional[str] = None,
    is_sensitive: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """本地 mock 过滤（仅 OPEN_EVENTS_SOURCE=mock 或回退时使用）。"""
    _validate_date_pair(start_date, end_date)

    # mock 模式：不传日期 → 默认演示窗口
    if start_date is None and end_date is None:
        start_date = DEFAULT_WINDOW_START
        end_date = DEFAULT_WINDOW_END

    start_dt = _parse_date(start_date, "start_date")
    end_dt = _parse_date(end_date, "end_date")

    if region is not None and region not in ("domestic", "foreign"):
        raise HTTPException(status_code=400, detail="region 非法，应为 domestic 或 foreign")

    kw = (keyword or "").strip().lower()
    items: List[Dict[str, Any]] = []
    for raw in MOCK_LIST_ITEMS:
        item = dict(raw)
        if is_sensitive is not None and bool(item.get("is_sensitive")) is not is_sensitive:
            continue
        if topic_id is not None and str(item.get("topic_id") or "") != str(topic_id):
            continue
        if region is not None and item.get("region") != region:
            continue
        ed = _parse_date(str(item.get("event_date")), "event_date")
        if ed < start_dt or ed > end_dt:
            continue
        if kw:
            blob = " ".join(
                str(item.get(k) or "")
                for k in ("title", "summary", "place_name")
            ).lower()
            if kw not in blob:
                continue
        items.append(item)

    items.sort(
        key=lambda x: (str(x.get("event_date") or ""), int(x.get("id") or 0)),
        reverse=True,
    )
    return items


def build_open_events_data(
    *,
    page: int = 1,
    page_size: int = 50,
    all_mode: bool = False,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    topic_id: Optional[str] = None,
    region: Optional[str] = None,
    keyword: Optional[str] = None,
    is_sensitive: Optional[bool] = None,
) -> Dict[str, Any]:
    """本地 mock 组装（单测 / OPEN_EVENTS_SOURCE=mock）。"""
    filtered = filter_open_events(
        start_date=start_date,
        end_date=end_date,
        topic_id=topic_id,
        region=region,
        keyword=keyword,
        is_sensitive=is_sensitive,
    )
    total = len(filtered)

    applied_start = start_date if start_date is not None else DEFAULT_WINDOW_START
    applied_end = end_date if end_date is not None else DEFAULT_WINDOW_END

    if all_mode:
        truncated = total > _ALL_MAX
        return {
            "total": total,
            "all": True,
            "truncated": truncated,
            "max_limit": _ALL_MAX,
            "is_sensitive": is_sensitive,
            "start_date": applied_start,
            "end_date": applied_end,
            "items": filtered[:_ALL_MAX],
        }

    page = max(1, page)
    page_size = min(200, max(1, page_size))
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "total": total,
        "all": False,
        "truncated": False,
        "max_limit": None,
        "page": page,
        "page_size": page_size,
        "is_sensitive": is_sensitive,
        "start_date": applied_start,
        "end_date": applied_end,
        "items": filtered[start:end],
    }


async def resolve_open_events_data(
    *,
    page: int = 1,
    page_size: int = 50,
    all_mode: bool = False,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    topic_id: Optional[str] = None,
    region: Optional[str] = None,
    keyword: Optional[str] = None,
    is_sensitive: Optional[bool] = None,
) -> Dict[str, Any]:
    """按配置选择 upstream 或 mock。"""
    _validate_date_pair(start_date, end_date)
    source = open_events_source()

    if source == "mock":
        return build_open_events_data(
            page=page,
            page_size=page_size,
            all_mode=all_mode,
            start_date=start_date,
            end_date=end_date,
            topic_id=topic_id,
            region=region,
            keyword=keyword,
            is_sensitive=is_sensitive,
        )

    try:
        data = await fetch_open_events_from_upstream(
            page=page,
            page_size=page_size,
            all_mode=all_mode,
            start_date=start_date,
            end_date=end_date,
            topic_id=topic_id,
            region=region,
            keyword=keyword,
            is_sensitive=is_sensitive,
        )
        # 保证筛参回显（上游若未带回则补上）
        if "is_sensitive" not in data:
            data["is_sensitive"] = is_sensitive
        return data
    except Exception as e:
        logger.warning(
            "open/events 上游失败 base={} err={!r} fallback={}",
            upstream_base_url(),
            e,
            fallback_mock_enabled(),
        )
        if not fallback_mock_enabled():
            raise HTTPException(
                status_code=502,
                detail=f"上游 open/events 不可用: {e}",
            ) from e
        return build_open_events_data(
            page=page,
            page_size=page_size,
            all_mode=all_mode,
            start_date=start_date,
            end_date=end_date,
            topic_id=topic_id,
            region=region,
            keyword=keyword,
            is_sensitive=is_sensitive,
        )


@router.get("/api/v1/open/events")
async def get_open_events(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    all: bool = Query(False, description="true 时全量，最多 1000 条"),
    start_date: Optional[str] = Query(
        None, description="事件日期起 YYYY-MM-DD；与 end 成对；都不传则由上游决定"
    ),
    end_date: Optional[str] = Query(
        None, description="事件日期止 YYYY-MM-DD；与 start 成对；都不传则由上游决定"
    ),
    topic_id: Optional[str] = Query(None, description="专题过滤"),
    region: Optional[str] = Query(None, description="domestic / foreign（透传上游）"),
    keyword: Optional[str] = Query(None, description="标题/摘要/地点模糊搜索"),
    is_sensitive: Optional[bool] = Query(None, description="敏感筛选；不传=全部"),
):
    """对外开放事件列表。默认代理 8250；ID 以上游为准。"""
    data = await resolve_open_events_data(
        page=page,
        page_size=page_size,
        all_mode=all,
        start_date=start_date,
        end_date=end_date,
        topic_id=topic_id,
        region=region,
        keyword=keyword,
        is_sensitive=is_sensitive,
    )
    return _ok(data)
