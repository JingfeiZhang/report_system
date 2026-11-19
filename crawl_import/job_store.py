# -*- coding: utf-8 -*-
"""导入任务进度落盘（JSON），供轮询查询。"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from crawl_import.config import extract_dir

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _jobs_dir() -> Path:
    d = extract_dir() / "_jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


def new_job_id() -> str:
    return uuid.uuid4().hex


def create_job(meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    job_id = new_job_id()
    payload = {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "message": "任务已创建，等待处理",
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
        "extract_dir": None,
        "manifest": None,
        "tables": [],
        "progress_percent": 0,
        "meta": meta or {},
    }
    save_job(payload)
    return payload


def save_job(job: Dict[str, Any]) -> None:
    job = dict(job)
    job["updated_at"] = _now()
    path = _job_path(job["job_id"])
    with _lock:
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def update_job(job_id: str, **fields: Any) -> Dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise KeyError(job_id)
    job.update(fields)
    save_job(job)
    return job


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    path = _job_path(job_id)
    if not path.exists():
        return None
    with _lock:
        return json.loads(path.read_text(encoding="utf-8"))
