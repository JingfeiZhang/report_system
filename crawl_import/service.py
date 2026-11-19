# -*- coding: utf-8 -*-
"""上传 zip → 后台解压 → 达梦入库。"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from crawl_import import job_store
from crawl_import.config import extract_dir
from crawl_import.importer import extract_zip, import_content_dir


def _job_work_dir(job_id: str) -> Path:
    d = extract_dir() / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue_upload(
    *,
    filename: str,
    content: bytes,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    job = job_store.create_job(meta=meta)
    job_id = job["job_id"]
    work = _job_work_dir(job_id)
    zip_path = work / (filename or "upload.zip")
    if not zip_path.name.lower().endswith(".zip"):
        zip_path = work / f"{zip_path.stem}.zip"
    zip_path.write_bytes(content)

    job_store.update_job(
        job_id,
        status="queued",
        phase="uploaded",
        message="文件已接收，排队解压入库",
        extract_dir=str(work),
        meta={**(meta or {}), "zip_name": zip_path.name, "zip_bytes": len(content)},
        progress_percent=5,
    )

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, zip_path, work),
        name=f"crawl-import-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return job_store.get_job(job_id) or job


def _run_job(job_id: str, zip_path: Path, work: Path) -> None:
    try:
        job_store.update_job(
            job_id,
            status="running",
            phase="extracting",
            message="正在解压",
            progress_percent=10,
        )
        unpack_root = work / "extracted"
        if unpack_root.exists():
            shutil.rmtree(unpack_root)
        content_dir = extract_zip(zip_path, unpack_root)

        manifest = None
        manifest_path = content_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("manifest 解析失败: {}", e)

        job_store.update_job(
            job_id,
            phase="extracted",
            message=f"解压完成: {content_dir}",
            extract_dir=str(content_dir),
            manifest=manifest,
            progress_percent=25,
        )

        def _progress(patch: Dict[str, Any]) -> None:
            job_store.update_job(job_id, status="running", **patch)

        result = import_content_dir(content_dir, progress_cb=_progress)
        written = int(result.get("rows_written") or 0)
        job_store.update_job(
            job_id,
            status="completed",
            phase="completed",
            message=f"解压并入库完成（写入 {written} 行）",
            tables=result.get("tables") or [],
            progress_percent=100,
            schema=result.get("schema"),
            rows_written=written,
        )
    except Exception as e:
        logger.exception("crawl_import job failed {}", job_id)
        job_store.update_job(
            job_id,
            status="failed",
            phase="failed",
            message="导入失败",
            error=str(e),
            progress_percent=100,
        )


def get_progress(job_id: str) -> Optional[Dict[str, Any]]:
    return job_store.get_job(job_id)
