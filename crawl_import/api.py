# -*- coding: utf-8 -*-
"""MindSpider 结果 zip 上传 / 进度查询 FastAPI 路由。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from crawl_import.config import max_upload_mb
from crawl_import import service as import_service

router = APIRouter(prefix="/api/v1/crawl-import", tags=["CrawlImport"])


def _ok(data: Any, msg: str = "success") -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": msg, "data": data})


@router.post("/jobs")
async def create_import_job(
    file: UploadFile = File(..., description="MindSpider 导出 zip（含 manifest.json + *.csv）"),
    event_id: Optional[str] = Form(None),
    event_date: Optional[str] = Form(None),
    remark: Optional[str] = Form(None),
):
    """上传 zip，后台解压到 CRAWL_IMPORT_EXTRACT_DIR 并 upsert 到达梦 mindspider_war。"""
    filename = file.filename or "upload.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="仅支持 .zip 文件")

    content = await file.read()
    limit = max_upload_mb() * 1024 * 1024
    if len(content) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大，超过限制 {max_upload_mb()}MB",
        )
    if not content:
        raise HTTPException(status_code=400, detail="空文件")

    meta = {
        "event_id": event_id,
        "event_date": event_date,
        "remark": remark,
        "original_filename": filename,
    }
    try:
        job = import_service.enqueue_upload(
            filename=filename,
            content=content,
            meta=meta,
        )
    except Exception as e:
        logger.exception("创建导入任务失败")
        raise HTTPException(status_code=500, detail=f"创建任务失败: {e}") from e

    return _ok(
        {
            "job_id": job["job_id"],
            "status": job.get("status"),
            "phase": job.get("phase"),
            "message": job.get("message"),
            "extract_dir": job.get("extract_dir"),
        },
        msg="queued",
    )


@router.get("/jobs/{job_id}")
async def get_import_job(job_id: str):
    """查询解压与入库进度。"""
    job = import_service.get_progress(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job 不存在")
    return _ok(job)
