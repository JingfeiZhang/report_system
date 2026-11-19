#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外开放事件接口客户端：支持HTTP 分页拉取。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("event_sync.api")


def fetch_from_file(path: str | Path) -> List[Dict[str, Any]]:
    """从本地 JSON 读取事件列表（结构与接口 items[] 一致）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"事件文件不存在: {p}")
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("本地事件文件顶层必须是 JSON 数组")
    logger.info("从本地文件读取事件: %s, 条数=%d", p, len(data))
    return data


def fetch_from_api(
    config: Dict[str, str],
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    topic_id: Optional[str] = None,
    region: Optional[str] = None,
    keyword: Optional[str] = None,
    page_size: int = 200,
) -> List[Dict[str, Any]]:
    """
    分页拉取 GET {API_BASE}/open/events。
    start_date / end_date 必须同时传或同时省略。
    """
    if (start_date is None) ^ (end_date is None):
        raise ValueError("start_date 与 end_date 需同时传或同时省略")

    base = config["API_BASE"].rstrip("/")
    timeout = int(config.get("API_TIMEOUT", "30"))
    url = f"{base}/open/events"

    all_items: List[Dict[str, Any]] = []
    page = 1

    while True:
        params: Dict[str, Any] = {
            "page": page,
            "page_size": page_size,
        }
        if start_date and end_date:
            params["start_date"] = start_date
            params["end_date"] = end_date
        if topic_id:
            params["topic_id"] = topic_id
        if region:
            params["region"] = region
        if keyword:
            params["keyword"] = keyword

        logger.info("请求事件接口 page=%s params=%s", page, params)
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()

        if body.get("code") != 0:
            raise RuntimeError(
                f"接口返回失败: code={body.get('code')} msg={body.get('msg')}"
            )

        data = body["data"]
        items = data.get("items") or []
        all_items.extend(items)

        total = int(data.get("total", 0))
        logger.info(
            "接口返回 page=%s items=%d total=%d 累计=%d",
            page,
            len(items),
            total,
            len(all_items),
        )
        if page * page_size >= total or not items:
            break
        page += 1

    return all_items


def group_titles_by_date(events: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """
    按 event_date 分组 title。
    - 跳过空 title / 空 event_date
    - 同日 title 去重，保留首次出现顺序
    """
    grouped: Dict[str, List[str]] = {}
    for ev in events:
        date_str = (ev.get("event_date") or "").strip()
        title = (ev.get("title") or "").strip()
        if not date_str or not title:
            continue
        bucket = grouped.setdefault(date_str, [])
        if title not in bucket:
            bucket.append(title)
    logger.info(
        "分组完成: 日期数=%d 标题数=%d",
        len(grouped),
        sum(len(v) for v in grouped.values()),
    )
    return grouped


def event_biz_id(ev: Dict[str, Any]) -> Optional[int]:
    """业务事件 ID：优先 id，其次 main_event_id / record_event_id。"""
    for key in ("id", "main_event_id", "record_event_id"):
        val = ev.get(key)
        if val is None or val == "":
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def group_events_by_date(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按 event_date 分组完整事件（同日同 id 去重）。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    seen: Dict[str, set] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        date_str = (ev.get("event_date") or "").strip()
        title = (ev.get("title") or "").strip()
        if not date_str or not title:
            continue
        eid = event_biz_id(ev)
        key = str(eid) if eid is not None else f"title:{title}"
        bucket_seen = seen.setdefault(date_str, set())
        if key in bucket_seen:
            continue
        bucket_seen.add(key)
        grouped.setdefault(date_str, []).append(ev)
    logger.info(
        "事件分组完成: 日期数=%d 事件数=%d",
        len(grouped),
        sum(len(v) for v in grouped.values()),
    )
    return grouped


def fetch_events(
    config: Dict[str, str],
    *,
    from_file: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    topic_id: Optional[str] = None,
    region: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, List[str]]:
    """统一入口：返回 {event_date: [titles...]}。"""
    if from_file:
        events = fetch_from_file(from_file)
    else:
        events = fetch_from_api(
            config,
            start_date=start_date,
            end_date=end_date,
            topic_id=topic_id,
            region=region,
            keyword=keyword,
        )
    return group_titles_by_date(events)