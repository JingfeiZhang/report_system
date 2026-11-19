# -*- coding: utf-8 -*-
"""调用 8250（平台）对外开放事件列表。

GET {OPEN_EVENTS_UPSTREAM_BASE_URL}/api/v1/open/events
事件 ID / 列表权威在上游；本服务只透传筛选与分页参数。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx
from loguru import logger

_DEFAULT_BASE = "http://192.168.1.157:8250"
_PATH = "/api/v1/open/events"


def _get_setting(name: str, default: Any = None) -> Any:
    try:
        from config import settings

        value = getattr(settings, name, None)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return os.getenv(name, default)


def upstream_base_url() -> str:
    return str(
        _get_setting("OPEN_EVENTS_UPSTREAM_BASE_URL")
        or _get_setting("PLATFORM_EVENTS_API_BASE_URL")
        or _DEFAULT_BASE
    ).rstrip("/")


def upstream_timeout_sec() -> float:
    try:
        return float(_get_setting("OPEN_EVENTS_UPSTREAM_TIMEOUT_SEC") or 15)
    except (TypeError, ValueError):
        return 15.0


def open_events_source() -> str:
    """upstream（默认）| mock。"""
    raw = str(_get_setting("OPEN_EVENTS_SOURCE") or "upstream").strip().lower()
    return raw if raw in ("upstream", "mock") else "upstream"


def fallback_mock_enabled() -> bool:
    raw = str(_get_setting("OPEN_EVENTS_FALLBACK_MOCK") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _build_params(
    *,
    page: int,
    page_size: int,
    all_mode: bool,
    start_date: Optional[str],
    end_date: Optional[str],
    topic_id: Optional[str],
    region: Optional[str],
    keyword: Optional[str],
    is_sensitive: Optional[bool],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if all_mode:
        params["all"] = "true"
    else:
        params["page"] = page
        params["page_size"] = page_size
    if start_date is not None:
        params["start_date"] = start_date
    if end_date is not None:
        params["end_date"] = end_date
    if topic_id is not None:
        params["topic_id"] = topic_id
    if region is not None:
        params["region"] = region
    if keyword is not None and str(keyword).strip():
        params["keyword"] = str(keyword).strip()
    if is_sensitive is not None:
        params["is_sensitive"] = "true" if is_sensitive else "false"
    return params


async def fetch_open_events_from_upstream(
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
    """请求上游并返回其 ``data`` 对象（已解包）。

    Raises:
        httpx.HTTPError: 网络/超时等
        RuntimeError: HTTP 非 2xx 或业务 code != 0 / 无 data
    """
    base = upstream_base_url()
    url = f"{base}{_PATH}"
    params = _build_params(
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
    timeout = upstream_timeout_sec()
    logger.info("open/events upstream GET {} params={}", url, params)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()

    if not isinstance(body, dict):
        raise RuntimeError(f"上游响应非 JSON 对象: {type(body)}")
    code = body.get("code")
    if code not in (0, "0", None):
        raise RuntimeError(f"上游业务失败 code={code} msg={body.get('msg')}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("上游响应缺少 data 对象")
    return data
