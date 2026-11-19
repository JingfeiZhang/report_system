#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向 .10 BettaFish opinion-crawl 回写采集进度，供卫星公司查询。"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("event_sync.progress")


def _truthy(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def progress_enabled(config: Dict[str, str]) -> bool:
    return _truthy(config.get("OPINION_PROGRESS_ENABLED", "false"))


def _base(config: Dict[str, str]) -> str:
    return (
        config.get("OPINION_PROGRESS_BASE")
        or "http://192.168.1.10:28000/api/v1/opinion-crawl"
    ).rstrip("/")


def _timeout(config: Dict[str, str]) -> int:
    return int(config.get("OPINION_PROGRESS_TIMEOUT") or config.get("API_TIMEOUT") or "60")


def sync_day_on_server(config: Dict[str, str], crawl_day: str) -> Optional[dict]:
    """让 .10 从左屏拉事件并落库为 pending（.10 若连不上左屏会 mock，优先用 sync_items）。"""
    if not progress_enabled(config):
        return None
    url = f"{_base(config)}/sync-day"
    logger.info("回写：sync-day %s -> %s", crawl_day, url)
    try:
        resp = requests.post(
            url, params={"event_date": crawl_day}, timeout=_timeout(config)
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        logger.exception("sync-day 失败 day=%s", crawl_day)
        return None
    if body.get("code") not in (0, None):
        logger.error("sync-day 业务失败: %s", body)
        return None
    data = body.get("data") or body
    logger.info("sync-day 完成: %s", data.get("progress") or data)
    return data


def sync_items(
    config: Dict[str, str],
    crawl_day: str,
    items: List[dict],
) -> Optional[dict]:
    """把本机已拉到的 open/events 推到 .10 落库（推荐）。"""
    if not progress_enabled(config):
        return None
    url = f"{_base(config)}/sync-items"
    payload = {
        "event_date": crawl_day,
        "items": items,
        "source": "event_sync",
    }
    logger.info("回写：sync-items day=%s items=%d -> %s", crawl_day, len(items), url)
    try:
        resp = requests.post(url, json=payload, timeout=_timeout(config))
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        logger.exception("sync-items 失败 day=%s", crawl_day)
        return None
    if body.get("code") not in (0, None):
        logger.error("sync-items 业务失败: %s", body)
        return None
    data = body.get("data") or body
    logger.info("sync-items 完成: %s", data.get("progress") or data)
    return data


def report_day(
    config: Dict[str, str],
    crawl_day: str,
    *,
    status: str,
    message: str = "",
    titles: Optional[List[str]] = None,
    only_statuses: Optional[List[str]] = None,
) -> Optional[dict]:
    if not progress_enabled(config):
        return None
    url = f"{_base(config)}/progress/report-day"
    payload = {
        "event_date": crawl_day,
        "status": status,
        "message": message,
    }
    if titles is not None:
        payload["titles"] = titles
    if only_statuses is not None:
        payload["only_statuses"] = only_statuses
    logger.info(
        "回写：report-day day=%s status=%s titles=%s",
        crawl_day,
        status,
        len(titles) if titles is not None else "ALL",
    )
    try:
        resp = requests.post(url, json=payload, timeout=_timeout(config))
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        logger.exception("report-day 失败 day=%s status=%s", crawl_day, status)
        return None
    if body.get("code") not in (0, None):
        logger.error("report-day 业务失败: %s", body)
        return None
    data = body.get("data") or body
    logger.info("report-day 完成: updated=%s progress=%s", data.get("updated"), data.get("progress"))
    return data


def report_event(
    config: Dict[str, str],
    *,
    event_type: str,
    event_id: int,
    status: str,
    message: str = "",
) -> Optional[dict]:
    """回写单条事件进度。"""
    if not progress_enabled(config):
        return None
    url = f"{_base(config)}/progress/report"
    et = (event_type or "monitor").strip().lower()
    payload: Dict[str, object] = {
        "event_type": et,
        "status": status,
        "message": message,
    }
    if et == "record":
        payload["record_event_id"] = int(event_id)
    else:
        payload["main_event_id"] = int(event_id)
    logger.info(
        "回写：report event_type=%s id=%s status=%s",
        et,
        event_id,
        status,
    )
    try:
        resp = requests.post(url, json=payload, timeout=_timeout(config))
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        logger.exception("report 失败 id=%s status=%s", event_id, status)
        return None
    if body.get("code") not in (0, None):
        logger.error("report 业务失败: %s", body)
        return None
    data = body.get("data") or body
    logger.info("report 完成: progress=%s", data.get("progress"))
    return data


def mark_ids(
    config: Dict[str, str],
    crawl_day: str,
    event_ids: List[int],
    *,
    status: str = "completed",
    message: str = "",
) -> Optional[dict]:
    """按事件 ID 批量回写（与结果目录数对齐）。"""
    if not progress_enabled(config):
        return None
    ids = sorted({int(x) for x in event_ids})
    if not ids:
        return None
    url = f"{_base(config)}/progress/mark-ids"
    payload = {
        "event_date": crawl_day,
        "status": status,
        "event_ids": ids,
        "message": message,
    }
    logger.info(
        "回写：mark-ids day=%s status=%s ids=%d -> %s",
        crawl_day,
        status,
        len(ids),
        url,
    )
    try:
        resp = requests.post(url, json=payload, timeout=_timeout(config))
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        logger.exception("mark-ids 失败 day=%s", crawl_day)
        return None
    if body.get("code") not in (0, None):
        logger.error("mark-ids 业务失败: %s", body)
        return None
    data = body.get("data") or body
    logger.info("mark-ids 完成: updated=%s progress=%s", data.get("updated"), data.get("progress"))
    return data