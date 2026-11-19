"""厂家舆情采集业务：按天拉左屏事件落库、进度汇总、MindSpider 采集。"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from opinion_crawl.store import CrawlTask, CrawlTaskStore, get_store

_RUN_LOCK = threading.Lock()
_DAY_LOCKS: Dict[str, threading.Lock] = {}
_DAY_LOCKS_GUARD = threading.Lock()


def _day_lock(event_date: str) -> threading.Lock:
    with _DAY_LOCKS_GUARD:
        if event_date not in _DAY_LOCKS:
            _DAY_LOCKS[event_date] = threading.Lock()
        return _DAY_LOCKS[event_date]


async def fetch_day_open_events(event_date: str) -> Dict[str, Any]:
    """按天一次拉取：监测=入参日；record=上游今天敏感看板。"""
    from event_report.open_events import filter_open_events
    from event_report.upstream_open_events import (
        fallback_mock_enabled,
        fetch_open_events_from_upstream,
        open_events_source,
    )

    source = open_events_source()
    if source == "mock":
        items = filter_open_events(start_date=event_date, end_date=event_date)
        return {
            "total": len(items),
            "truncated": False,
            "items": items,
            "source": "mock",
        }

    try:
        data = await fetch_open_events_from_upstream(
            all_mode=True,
            start_date=event_date,
            end_date=event_date,
            is_sensitive=None,
        )
        data = dict(data)
        data["source"] = "upstream"
        return data
    except Exception as exc:
        if not fallback_mock_enabled():
            raise
        logger.warning("按天拉 open/events 失败，回退 mock: {}", exc)
        items = filter_open_events(start_date=event_date, end_date=event_date)
        return {
            "total": len(items),
            "truncated": False,
            "items": items,
            "source": "mock_fallback",
        }


async def sync_day(
    event_date: str,
    *,
    store: Optional[CrawlTaskStore] = None,
) -> Dict[str, Any]:
    """拉取当天应采事件并幂等落库。"""
    store = store or get_store()
    upstream = await fetch_day_open_events(event_date)
    items = list(upstream.get("items") or [])
    truncated = bool(upstream.get("truncated"))
    upstream_total = int(upstream.get("total") or len(items))

    if truncated:
        items = await _fetch_all_pages(event_date)
        upstream_total = len(items)
        truncated = False

    upserted = 0
    errors: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            store.upsert_from_open_item(item, crawl_day=event_date)
            upserted += 1
        except Exception as exc:
            errors.append(str(exc))

    counts = store.count_by_date(event_date)
    return {
        "event_date": event_date,
        "upstream_total": upstream_total,
        "upstream_source": upstream.get("source"),
        "upserted": upserted,
        "truncated": truncated,
        "errors": errors[:10],
        "progress": {
            "event_date": event_date,
            "total": counts["total"],
            "completed": counts["completed"],
            "pending": counts["pending"],
            "failed": counts["failed"],
        },
    }


def sync_items(
    event_date: str,
    items: List[Dict[str, Any]],
    *,
    store: Optional[CrawlTaskStore] = None,
    source: str = "event_sync",
) -> Dict[str, Any]:
    """本机 event_sync 已拉到的 open/events 条目直接落库（不依赖 .10 再拉左屏）。"""
    store = store or get_store()
    upserted = 0
    errors: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            store.upsert_from_open_item(item, crawl_day=event_date)
            upserted += 1
        except Exception as exc:
            errors.append(str(exc))
    counts = store.count_by_date(event_date)
    return {
        "event_date": event_date,
        "upstream_total": len(items),
        "upstream_source": source,
        "upserted": upserted,
        "truncated": False,
        "errors": errors[:10],
        "progress": {
            "event_date": event_date,
            "total": counts["total"],
            "completed": counts["completed"],
            "pending": counts["pending"],
            "failed": counts["failed"],
        },
    }


async def _fetch_all_pages(event_date: str) -> List[Dict[str, Any]]:
    from event_report.upstream_open_events import fetch_open_events_from_upstream

    page = 1
    page_size = 200
    all_items: List[Dict[str, Any]] = []
    while True:
        data = await fetch_open_events_from_upstream(
            page=page,
            page_size=page_size,
            all_mode=False,
            start_date=event_date,
            end_date=event_date,
            is_sensitive=None,
        )
        batch = list(data.get("items") or [])
        all_items.extend(x for x in batch if isinstance(x, dict))
        total = int(data.get("total") or 0)
        if page * page_size >= total or not batch:
            break
        page += 1
        if page > 50:
            break
    return all_items


def progress_by_date(
    event_date: str,
    *,
    store: Optional[CrawlTaskStore] = None,
) -> Dict[str, Any]:
    store = store or get_store()
    counts = store.count_by_date(event_date)
    return {
        "event_date": event_date,
        "total": counts["total"],
        "completed": counts["completed"],
        "pending": counts["pending"],
        "failed": counts["failed"],
    }


def progress_by_event(
    *,
    event_type: str,
    main_event_id: Optional[int] = None,
    record_event_id: Optional[int] = None,
    store: Optional[CrawlTaskStore] = None,
) -> Dict[str, Any]:
    store = store or get_store()
    et = (event_type or "").strip().lower()
    if et == "monitor":
        if main_event_id is None:
            raise ValueError("monitor 须传 main_event_id")
        event_id = int(main_event_id)
    elif et == "record":
        if record_event_id is None:
            raise ValueError("record 须传 record_event_id")
        event_id = int(record_event_id)
    else:
        raise ValueError("event_type 须为 monitor 或 record")

    task = store.get(et, event_id)
    if task is None:
        return {
            "event_type": et,
            "main_event_id": event_id if et == "monitor" else None,
            "record_event_id": event_id if et == "record" else None,
            "event_date": None,
            "status": "unknown",
            "message": "本地无此采集任务，请先按天 sync",
        }
    return task.to_progress_dict()


def report_event_progress(
    *,
    event_type: str,
    status: str,
    main_event_id: Optional[int] = None,
    record_event_id: Optional[int] = None,
    message: str = "",
    store: Optional[CrawlTaskStore] = None,
) -> Dict[str, Any]:
    """外网爬虫回写单条进度。"""
    store = store or get_store()
    et = (event_type or "").strip().lower()
    if et == "monitor":
        if main_event_id is None:
            raise ValueError("monitor 须传 main_event_id")
        event_id = int(main_event_id)
    elif et == "record":
        if record_event_id is None:
            raise ValueError("record 须传 record_event_id")
        event_id = int(record_event_id)
    else:
        raise ValueError("event_type 须为 monitor 或 record")

    task = store.update_status(et, event_id, status=status, message=message or "")
    if task is None:
        raise ValueError("本地无此采集任务，请先 sync-day")
    return {
        "updated": 1,
        "task": task.to_progress_dict(),
        "progress": progress_by_date(task.event_date, store=store),
    }


def report_day_progress(
    event_date: str,
    *,
    status: str,
    message: str = "",
    titles: Optional[List[str]] = None,
    only_statuses: Optional[List[str]] = None,
    store: Optional[CrawlTaskStore] = None,
) -> Dict[str, Any]:
    """外网爬虫按采集日回写进度。

    - 传 titles：只更新标题匹配的任务
    - 不传 titles：更新该日全部（可限定 only_statuses）
    """
    store = store or get_store()
    if titles:
        n = store.update_status_by_titles(
            event_date, titles, status=status, message=message or ""
        )
    else:
        n = store.update_status_by_date(
            event_date,
            status=status,
            message=message or "",
            only_statuses=only_statuses,
        )
    return {
        "event_date": event_date,
        "updated": n,
        "status": status,
        "progress": progress_by_date(event_date, store=store),
    }


def mark_ids_progress(
    event_date: str,
    *,
    status: str,
    event_ids: List[int],
    message: str = "",
    store: Optional[CrawlTaskStore] = None,
) -> Dict[str, Any]:
    """按业务事件 ID 批量回写进度（与结果文件夹 20260809-{id} 对齐）。"""
    store = store or get_store()
    n = store.update_status_by_event_ids(
        event_date, event_ids, status=status, message=message or ""
    )
    return {
        "event_date": event_date,
        "updated": n,
        "requested": len(set(int(x) for x in event_ids)),
        "status": status,
        "progress": progress_by_date(event_date, store=store),
    }


def mindspider_trigger_enabled() -> bool:
    """默认开启：已接线 MindSpider。可用 env 关掉。"""
    raw = (os.getenv("OPINION_CRAWL_TRIGGER_MINDSPIDER") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def auto_complete_placeholder() -> bool:
    """跳过爬虫、直接标 completed（仅联调）。"""
    raw = (os.getenv("OPINION_CRAWL_PLACEHOLDER_COMPLETE") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def batch_mode_enabled() -> bool:
    """True：当日 pending 关键词合并一次爬；False：逐事件爬。"""
    raw = (os.getenv("OPINION_CRAWL_BATCH_MODE") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _task_keywords(task: CrawlTask) -> List[str]:
    kws = [str(k).strip() for k in (task.keywords or []) if str(k).strip()]
    if kws:
        return kws[:6]
    if task.title.strip():
        return [task.title.strip()[:40]]
    return []


def run_day_crawl(
    event_date: str,
    *,
    limit: int = 50,
    store: Optional[CrawlTaskStore] = None,
    background: bool = True,
) -> Dict[str, Any]:
    """触发某日待采任务。

    - 默认后台线程跑 MindSpider，接口立即返回 running
    - OPINION_CRAWL_PLACEHOLDER_COMPLETE=true：不爬，直接 completed
    - OPINION_CRAWL_TRIGGER_MINDSPIDER=false：只入队 running，不调爬虫
    - background=False：同步执行（单测 / 脚本）
    """
    store = store or get_store()
    tasks = store.list_pending_by_date(event_date, limit=limit)
    if not tasks:
        return {
            "event_date": event_date,
            "triggered": 0,
            "mode": "idle",
            "mindspider_enabled": mindspider_trigger_enabled(),
            "placeholder_complete": auto_complete_placeholder(),
            "items": [],
            "progress": progress_by_date(event_date, store=store),
        }

    # 占位完成：不调爬虫
    if auto_complete_placeholder():
        started: List[Dict[str, Any]] = []
        for task in tasks:
            store.update_status(
                task.event_type,
                task.event_id,
                status="completed",
                message="占位完成（OPINION_CRAWL_PLACEHOLDER_COMPLETE）",
            )
            refreshed = store.get(task.event_type, task.event_id)
            if refreshed:
                started.append(refreshed.to_progress_dict())
        return {
            "event_date": event_date,
            "triggered": len(started),
            "mode": "placeholder",
            "mindspider_enabled": mindspider_trigger_enabled(),
            "placeholder_complete": True,
            "items": started,
            "progress": progress_by_date(event_date, store=store),
        }

    # 先全部标 running
    keys: List[Tuple[str, int]] = []
    started_items: List[Dict[str, Any]] = []
    for task in tasks:
        store.update_status(
            task.event_type,
            task.event_id,
            status="running",
            message=(
                "MindSpider 采集中…"
                if mindspider_trigger_enabled()
                else "已入队（MindSpider 触发关闭）"
            ),
        )
        keys.append((task.event_type, task.event_id))
        refreshed = store.get(task.event_type, task.event_id)
        if refreshed:
            started_items.append(refreshed.to_progress_dict())

    if not mindspider_trigger_enabled():
        return {
            "event_date": event_date,
            "triggered": len(started_items),
            "mode": "queued_only",
            "mindspider_enabled": False,
            "placeholder_complete": False,
            "items": started_items,
            "progress": progress_by_date(event_date, store=store),
        }

    mode = "batch" if batch_mode_enabled() else "per_task"
    if background:
        t = threading.Thread(
            target=_background_run,
            kwargs={
                "event_date": event_date,
                "keys": keys,
                "batch": batch_mode_enabled(),
            },
            name=f"opinion-crawl-{event_date}",
            daemon=True,
        )
        t.start()
        return {
            "event_date": event_date,
            "triggered": len(started_items),
            "mode": f"async_{mode}",
            "mindspider_enabled": True,
            "placeholder_complete": False,
            "message": "已后台启动 MindSpider，请轮询 progress 接口",
            "items": started_items,
            "progress": progress_by_date(event_date, store=store),
        }

    _background_run(event_date=event_date, keys=keys, batch=batch_mode_enabled())
    final_items = []
    for et, eid in keys:
        task = store.get(et, eid)
        if task:
            final_items.append(task.to_progress_dict())
    return {
        "event_date": event_date,
        "triggered": len(final_items),
        "mode": f"sync_{mode}",
        "mindspider_enabled": True,
        "placeholder_complete": False,
        "items": final_items,
        "progress": progress_by_date(event_date, store=store),
    }


def _background_run(
    *,
    event_date: str,
    keys: List[Tuple[str, int]],
    batch: bool,
) -> None:
    lock = _day_lock(event_date)
    if not lock.acquire(blocking=False):
        logger.warning("opinion_crawl day={} 已有采集线程在跑，跳过", event_date)
        return
    try:
        store = get_store()
        if batch:
            _run_batch(store, keys)
        else:
            for et, eid in keys:
                task = store.get(et, eid)
                if not task:
                    continue
                _run_one(store, task)
    finally:
        lock.release()


def _run_one(store: CrawlTaskStore, task: CrawlTask) -> None:
    from opinion_crawl.mindspider_runner import crawl_keywords

    kws = _task_keywords(task)
    if not kws:
        store.update_status(
            task.event_type,
            task.event_id,
            status="failed",
            message="无可用关键词",
        )
        return
    try:
        result = crawl_keywords(kws)
    except Exception as exc:
        logger.exception("MindSpider 单任务失败: {}", exc)
        store.update_status(
            task.event_type,
            task.event_id,
            status="failed",
            message=str(exc)[:300],
        )
        return

    if result.get("success"):
        notes = result.get("total_notes")
        msg = "MindSpider 采集完成"
        if notes is not None:
            msg = f"MindSpider 采集完成（内容约 {notes} 条）"
        if result.get("dry_run"):
            msg = "MindSpider dry-run 成功"
        store.update_status(
            task.event_type,
            task.event_id,
            status="completed",
            message=msg,
        )
    else:
        err = result.get("error") or result.get("stderr_tail") or "爬取失败"
        store.update_status(
            task.event_type,
            task.event_id,
            status="failed",
            message=str(err)[:300],
        )


def _run_batch(store: CrawlTaskStore, keys: List[Tuple[str, int]]) -> None:
    from opinion_crawl.mindspider_runner import crawl_keywords

    tasks: List[CrawlTask] = []
    all_kws: List[str] = []
    seen = set()
    for et, eid in keys:
        task = store.get(et, eid)
        if not task:
            continue
        tasks.append(task)
        for k in _task_keywords(task):
            if k not in seen:
                seen.add(k)
                all_kws.append(k)

    if not all_kws:
        for task in tasks:
            store.update_status(
                task.event_type,
                task.event_id,
                status="failed",
                message="无可用关键词",
            )
        return

    # 限制合并词量，避免一次过大
    all_kws = all_kws[:30]
    try:
        result = crawl_keywords(all_kws)
    except Exception as exc:
        logger.exception("MindSpider 批量失败: {}", exc)
        for task in tasks:
            store.update_status(
                task.event_type,
                task.event_id,
                status="failed",
                message=str(exc)[:300],
            )
        return

    if result.get("success"):
        msg = "MindSpider 批量采集完成"
        if result.get("dry_run"):
            msg = "MindSpider 批量 dry-run 成功"
        status = "completed"
    else:
        msg = str(result.get("error") or "批量爬取失败")[:300]
        status = "failed"

    for task in tasks:
        store.update_status(
            task.event_type,
            task.event_id,
            status=status,
            message=msg,
        )
