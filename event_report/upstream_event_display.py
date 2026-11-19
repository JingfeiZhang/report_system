# -*- coding: utf-8 -*-
"""调用 8250 事件详情 display，供 sensitive-analysis 补全上游 ID。

GET {base}/api/v1/event-report/display
  monitor: ?event_type=monitor&main_event_id=
  record:  ?event_type=record&record_event_id=
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

import httpx
from loguru import logger

from event_report.upstream_open_events import upstream_base_url, upstream_timeout_sec

_PATH = "/api/v1/event-report/display"


async def fetch_event_display(
    event_id: Union[int, str],
    *,
    event_type: str,
) -> Dict[str, Any]:
    """返回上游 display 的 data 对象。失败抛异常。"""
    et = (event_type or "monitor").strip().lower()
    params: Dict[str, Any] = {"event_type": et}
    if et == "record":
        params["record_event_id"] = event_id
    else:
        params["main_event_id"] = event_id

    url = f"{upstream_base_url()}{_PATH}"
    timeout = upstream_timeout_sec()
    logger.info("event-report/display GET {} params={}", url, params)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()

    if not isinstance(body, dict):
        raise RuntimeError(f"display 响应非对象: {type(body)}")
    if body.get("code") not in (0, "0", None):
        raise RuntimeError(f"display 业务失败 code={body.get('code')} msg={body.get('msg')}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("display 缺少 data")
    return data
