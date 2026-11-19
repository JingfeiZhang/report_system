"""
将 BettaFish 分析相关 loguru 日志截断后转发到 TaskEventBus（SSE agent_log 事件）。

环境变量:
  SSE_LOG_ENABLED   默认 true，设为 false 可关闭
  SSE_LOG_MAX_CHARS 默认 800；设为 0 表示不截断（全文）
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi_integration import AnalysisTask

# 单任务绑定：同一进程同时只跑一个分析任务时足够；多任务并发时日志可能混入同一 SSE
_active_task: Optional["AnalysisTask"] = None
_active_task_lock = threading.Lock()

_log_sink_id: Optional[int] = None

SSE_LOG_ENABLED = os.getenv("SSE_LOG_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SSE_LOG_MAX_CHARS = int(os.getenv("SSE_LOG_MAX_CHARS", "800"))

LOG_STREAM_LEVELS = frozenset({"INFO", "WARNING", "ERROR", "CRITICAL"})

# 模块名/path 含以下片段才转发（避免 uvicorn/httpx 等刷屏）
AGENT_LOG_MODULE_MARKERS = (
    "InsightEngine",
    "MediaEngine",
    "QueryEngine",
    "ReportEngine",
    "ForumEngine",
)

ENGINE_MARKERS = (
    ("InsightEngine", "insight"),
    ("MediaEngine", "media"),
    ("QueryEngine", "query"),
    ("ReportEngine", "report"),
    ("ForumEngine", "forum"),
)


def _resolve_engine(module_name: str, file_path: str) -> str:
    combined = f"{module_name}:{file_path}"
    for marker, engine in ENGINE_MARKERS:
        if marker in combined:
            return engine
    return "system"


def _should_forward(record: Dict[str, Any]) -> bool:
    if not SSE_LOG_ENABLED:
        return False
    level_name = record["level"].name
    if level_name not in LOG_STREAM_LEVELS:
        return False
    module_name = record.get("name") or record.get("module") or ""
    file_path = record.get("file", {}).path if record.get("file") else ""
    combined = f"{module_name}:{file_path}"
    return any(marker in combined for marker in AGENT_LOG_MODULE_MARKERS)


def _truncate_message(message: str) -> tuple[str, bool, int]:
    original_len = len(message)
    if SSE_LOG_MAX_CHARS <= 0 or original_len <= SSE_LOG_MAX_CHARS:
        return message, False, original_len
    return message[:SSE_LOG_MAX_CHARS] + "…", True, original_len


@contextmanager
def bind_log_stream_task(task: "AnalysisTask"):
    """在分析任务生命周期内绑定 SSE 日志转发目标。"""
    global _active_task
    with _active_task_lock:
        _active_task = task
    try:
        yield
    finally:
        with _active_task_lock:
            _active_task = None


def _loguru_sink(message) -> None:
    """loguru sink：截断后 emit agent_log，异常静默以防递归。"""
    try:
        record = message.record
        if not _should_forward(record):
            return

        with _active_task_lock:
            task = _active_task

        if task is None:
            return
        status_value = getattr(task.status, "value", task.status)
        if status_value not in ("pending", "running"):
            return

        level_name = record["level"].name
        raw_message = record["message"]
        text, truncated, original_length = _truncate_message(str(raw_message))
        module_name = record.get("name") or record.get("module") or ""
        file_obj = record.get("file")
        file_path = getattr(file_obj, "path", "") if file_obj else ""
        engine = _resolve_engine(module_name, file_path)
        timestamp = record["time"].strftime("%H:%M:%S.%f")[:-3]

        line = f"[{timestamp}] [{level_name}] [{engine}] {text}"
        payload: Dict[str, Any] = {
            "line": line,
            "level": level_name.lower(),
            "engine": engine,
            "message": text,
            "module": module_name,
            "function": record.get("function", ""),
            "truncated": truncated,
            "original_length": original_length,
        }
        task.emit_event("agent_log", payload)
    except Exception:
        pass


def setup_analysis_log_stream_sink() -> None:
    """进程内只注册一次 loguru sink。"""
    global _log_sink_id
    if _log_sink_id is not None:
        return
    from loguru import logger

    sink_level = (
        os.getenv("SSE_LOG_LEVEL")
        or os.getenv("LOG_LEVEL")
        or os.getenv("LOGURU_LEVEL")
        or "INFO"
    ).strip().upper()
    _log_sink_id = logger.add(
        _loguru_sink,
        level=sink_level,
        enqueue=False,
        catch=True,
    )
