"""厂家舆情采集 FastAPI 路由（挂到 BettaFish fastapi_integration）。

对接文档要求厂家提供：
- 按日进度
- 按事件进度

本模块另提供：按天从左屏 /open/events 同步落库、触发采集（MindSpider 可后接）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from opinion_crawl import service as crawl_service
from opinion_crawl.store import get_store

router = APIRouter(prefix="/api/v1/opinion-crawl", tags=["OpinionCrawl"])


def _ok(data: Any, msg: str = "success") -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": msg, "data": data})


def _parse_day(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="event_date 格式应为 YYYY-MM-DD") from e
    return value


class ReportEventBody(BaseModel):
    event_type: str = Field(..., description="monitor | record")
    status: str = Field(..., description="pending|running|completed|failed|skipped")
    main_event_id: Optional[int] = None
    record_event_id: Optional[int] = None
    message: str = ""


class ReportDayBody(BaseModel):
    event_date: str = Field(..., description="采集日 YYYY-MM-DD")
    status: str = Field(..., description="pending|running|completed|failed|skipped")
    message: str = ""
    titles: Optional[List[str]] = Field(
        None, description="若提供则只更新这些标题对应的任务"
    )
    only_statuses: Optional[List[str]] = Field(
        None, description="不传 titles 时，仅更新处于这些状态的任务"
    )


class SyncItemsBody(BaseModel):
    event_date: str = Field(..., description="采集日 YYYY-MM-DD")
    items: List[dict] = Field(
        ..., description="左屏 open/events 条目（与接口 items[] 同结构）"
    )
    source: str = Field("event_sync", description="来源标记")


class MarkIdsBody(BaseModel):
    event_date: str = Field(..., description="采集日 YYYY-MM-DD")
    status: str = Field(
        "completed",
        description="pending|running|completed|failed|skipped；默认 completed",
    )
    event_ids: List[int] = Field(
        ..., description="业务事件 ID 列表（与结果目录名 20260809-{id} 中的 id 一致）"
    )
    message: str = ""


@router.get("/progress/by-date")
async def progress_by_date(
    event_date: str = Query(..., description="自然日 YYYY-MM-DD，与按天拉取/落库日一致"),
):
    """按日查询采集进度（厂家须提供，供左屏/前端查询）。"""
    day = _parse_day(event_date)
    return _ok(crawl_service.progress_by_date(day))


@router.get("/progress/by-event")
async def progress_by_event(
    event_type: str = Query(..., description="monitor | record"),
    main_event_id: Optional[int] = Query(None, description="监测事件 ID"),
    record_event_id: Optional[int] = Query(None, description="敏感记录事件 ID"),
):
    """按事件查询采集进度。"""
    try:
        data = crawl_service.progress_by_event(
            event_type=event_type,
            main_event_id=main_event_id,
            record_event_id=record_event_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _ok(data)


@router.post("/sync-day")
async def sync_day(
    event_date: str = Query(..., description="按天拉取并落库的自然日 YYYY-MM-DD"),
):
    """从左屏开放事件列表按天同步到厂家本地任务库。"""
    day = _parse_day(event_date)
    try:
        data = await crawl_service.sync_day(day)
    except Exception as e:
        logger.exception("opinion-crawl sync-day failed: {}", e)
        raise HTTPException(status_code=502, detail=f"拉取/落库失败: {e}") from e
    return _ok(data)


@router.post("/sync-items")
async def sync_items(body: SyncItemsBody):
    """外网 event_sync 推送已拉取的事件列表落库（推荐；不依赖 .10 再访问左屏）。"""
    day = _parse_day(body.event_date)
    try:
        data = crawl_service.sync_items(
            day, list(body.items or []), source=body.source or "event_sync"
        )
    except Exception as e:
        logger.exception("opinion-crawl sync-items failed: {}", e)
        raise HTTPException(status_code=502, detail=f"落库失败: {e}") from e
    return _ok(data)


@router.post("/run-day")
async def run_day(
    event_date: str = Query(..., description="触发该日落库中 pending/failed 任务"),
    limit: int = Query(50, ge=1, le=500),
    sync: bool = Query(
        False,
        description="true=同步跑完再返回（调试）；默认后台异步调 MindSpider",
    ),
):
    """触发某日采集：默认后台调用 MindSpider，请轮询 progress。"""
    day = _parse_day(event_date)
    data = crawl_service.run_day_crawl(day, limit=limit, background=not sync)
    return _ok(data)


@router.get("/tasks")
async def list_tasks(
    event_date: str = Query(..., description="落库采集日 YYYY-MM-DD"),
):
    """调试：列出某日落库任务明细。"""
    day = _parse_day(event_date)
    tasks = get_store().list_by_date(day)
    return _ok(
        {
            "event_date": day,
            "total": len(tasks),
            "items": [t.to_progress_dict() for t in tasks],
        }
    )


@router.post("/progress/report")
async def report_event(body: ReportEventBody):
    """外网爬虫回写单条事件进度（供 event_sync 等对接）。"""
    try:
        data = crawl_service.report_event_progress(
            event_type=body.event_type,
            status=body.status,
            main_event_id=body.main_event_id,
            record_event_id=body.record_event_id,
            message=body.message,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _ok(data)


@router.post("/progress/report-day")
async def report_day(body: ReportDayBody):
    """外网爬虫按采集日回写进度（可按 titles 过滤）。"""
    day = _parse_day(body.event_date)
    try:
        data = crawl_service.report_day_progress(
            day,
            status=body.status,
            message=body.message,
            titles=body.titles,
            only_statuses=body.only_statuses,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _ok(data)


@router.post("/progress/mark-ids")
async def mark_ids(body: MarkIdsBody):
    """按事件 ID 批量回写进度。completed 条数应与成功落盘的结果文件夹数对齐。"""
    day = _parse_day(body.event_date)
    try:
        data = crawl_service.mark_ids_progress(
            day,
            status=body.status,
            event_ids=body.event_ids,
            message=body.message,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _ok(data)
