"""
FastAPI集成接口 - 用于与外部大系统集成

该模块提供了FastAPI接口，用于接收前端事件、协调执行三个分析引擎、
启动ForumEngine监控、生成综合报告，并返回报告生成完毕信号。

完整的执行流程：
1. 接收分析事件
2. 并发启动 InsightEngine、QueryEngine、MediaEngine 执行分析
3. 启动 ForumEngine 监控日志
4. 等待三个引擎完成并生成报告
5. 调用 ReportEngine 生成综合报告
6. 返回报告生成完毕信号

启动顺序:
1. 先启动 mock_search_api/main.py (搜索API服务)
2. 再启动本接口服务

使用方式:
    uvicorn fastapi_integration:app --reload --port 8000

流式进度（A 层 + agent_log 截断日志）:
    GET /api/v1/analysis/stream/{task_id}  (text/event-stream)
    环境变量 SSE_LOG_MAX_CHARS=800（0 为不截断），SSE_LOG_ENABLED=false 可关闭 agent_log

报告下载:
    GET /api/v1/analysis/report/{task_id}            HTML 预览（测试用）
    GET /api/v1/analysis/report/{task_id}/markdown   Markdown 正式交付物（任务完成后预生成）
    GET /api/v1/analysis/report/{task_id}/pdf        PDF（可选，ENABLE_PDF_EXPORT=true）

统一 SSE（v2）:
    POST /api/v1/analysis/event  Accept: text/event-stream  → 创建任务并 SSE 至结束
    GET  /api/v1/analysis/stream/{task_id}                 → 断线续传（Last-Event-ID）

快模式 singleAgent（3～5 分钟）:
    POST /api/v1/analysis/event2  Accept: text/event-stream  → 达梦检索 + 流式 Markdown
"""

import os
import sys
import json
import time
import uuid
import asyncio
import threading
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List, Union
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 尽早加载 .env，使下方 os.getenv 能读到 ANALYSIS_ENGINE_MAX_WAIT_SECONDS 等项
try:
    from dotenv import load_dotenv
    _env_file = Path.cwd() / ".env"
    if not _env_file.exists():
        _env_file = PROJECT_ROOT / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass

# 设置UTF-8编码
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONUTF8'] = '1'
os.environ['PYTHONUNBUFFERED'] = '1'

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from pydantic import BaseModel, Field, ConfigDict, model_validator, AliasChoices
from loguru import logger
from contextlib import asynccontextmanager
from queue import Empty

# 日志等级：TRACE < DEBUG < INFO < SUCCESS < WARNING < ERROR < CRITICAL
# 拉高默认到 WARNING，docker logs 里更容易看到 stop/cancel 等关键事件；需要细查时设 LOG_LEVEL=INFO/DEBUG
_LOG_LEVEL = (os.getenv("LOG_LEVEL") or os.getenv("LOGURU_LEVEL") or "WARNING").strip().upper()
if _LOG_LEVEL not in {
    "TRACE",
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
}:
    _LOG_LEVEL = "WARNING"
logger.remove()
logger.add(
    sys.stderr,
    level=_LOG_LEVEL,
    enqueue=True,
    backtrace=False,
    diagnose=False,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
)
logger.warning("loguru level={}", _LOG_LEVEL)

from task_event_bus import (
    TaskEventBus,
    STREAM_HEARTBEAT_INTERVAL,
    STREAM_TERMINAL_TYPES,
)
from analysis_log_stream import bind_log_stream_task, setup_analysis_log_stream_sink
from decision_sse import (
    DecisionSSETranslator,
    DecisionStreamContext,
    build_markdown_stream_events,
    format_decision_ping,
    format_decision_sse,
)
from workflow_events import (
    build_workflow_payload,
    workflow_from_engine_progress,
    workflow_from_report_event,
    workflow_from_task_message,
    should_emit_report_stage,
    scrub_report_stage_payload,
)

# ==================== 配置 ====================

# API服务配置
API_HOST = "127.0.0.1"
API_PORT = 8000
API_TITLE = "BettaFish Analysis System Integration API"
API_VERSION = "1.0.0"

# mock_search_api服务地址（Docker 同网部署时设 MOCK_SEARCH_API_HOST=mock-search-api）
MOCK_SEARCH_API_HOST = os.getenv("MOCK_SEARCH_API_HOST", "127.0.0.1")
MOCK_SEARCH_API_PORT = int(os.getenv("MOCK_SEARCH_API_PORT", "8787"))

# 等待 insight/media/query 三引擎完成的最长时间（秒），默认 3600（60 分钟）
ANALYSIS_ENGINE_MAX_WAIT_SECONDS = int(os.getenv("ANALYSIS_ENGINE_MAX_WAIT_SECONDS", "3600"))

# Markdown 为默认交付物；PDF 仅在显式开启时生成
ENABLE_PDF_EXPORT = os.getenv("ENABLE_PDF_EXPORT", "false").lower() in ("true", "1", "yes")
# result 阶段 Markdown 流式分块大小（字）
MARKDOWN_STREAM_CHUNK_SIZE = int(os.getenv("MARKDOWN_STREAM_CHUNK_SIZE", "12"))

# 引擎输出目录
ENGINE_OUTPUT_DIRS = {
    'insight': 'insight_engine_streamlit_reports',
    'media': 'media_engine_streamlit_reports',
    'query': 'query_engine_streamlit_reports'
}

# ==================== Pydantic 模型 ====================

# 前端附件允许格式
ALLOWED_ATTACHMENT_TYPES = frozenset({"docx", "xlsx", "pdf", "png", "jpg", "jpeg"})


class FileDataItem(BaseModel):
    """前端上传附件（files_data 数组元素）。"""
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field("", description="文件名（含后缀）")
    type: str = Field("", description="文件格式：docx/xlsx/pdf/png/jpg")
    content: str = Field(
        "",
        description="文件正文或图片识别/描述文本",
        validation_alias=AliasChoices("content", "cont"),
    )


class AnalysisEvent(BaseModel):
    """归一化后的内部分析事件（由 AnalysisEventRequest 转换）。"""
    event_id: str = Field(..., description="事件唯一标识")
    event_type: str = Field(..., description="事件类型，如 'analysis_request'")
    topic: str = Field(..., description="分析主题/话题（短标题）")
    query: Optional[str] = Field(None, description="分析查询词（完整指令，传给各引擎）")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="附加元数据")


class AnalysisEventRequest(BaseModel):
    """
    POST /api/v1/analysis/event 请求体（辅助决策前端）。

    字段说明：
    - instruction: 用户输入
    - session_id: 会话 ID
    - knowledge_base_ids: 知识库 dataset_id 列表，如 ["4ead8d0665fe11f196d82978a39a5161"]
    - ismodel: 1=启用模板（右屏推全部模板），2=灵活讨论（不推模板）；与 ext 无关
    - ext: 右屏勾选后带回；空=mode1 讨论；非空（如 selected_knowledge_ids）= mode0 正式报告
    - files_data: 附件列表（docx/xlsx/pdf/png/jpg）
    - model_config: { id, base_url, api_key }
    """
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    instruction: Optional[str] = Field(None, description="用户输入")
    session_id: Optional[str] = Field(None, description="会话 ID")
    request_id: Optional[str] = Field(None, description="本轮生成请求 ID，每轮唯一")
    source_request_id: Optional[str] = Field(None, description="被本轮抢占的上一请求 ID")
    knowledge_base_ids: List[Union[int, str]] = Field(
        default_factory=list,
        description="知识库 dataset_id 列表（原始字符串，非平台整数序号）",
    )
    ismodel: int = Field(
        default=2,
        description="1=启用模板（推全部模板到右屏），2=灵活讨论（不推模板）。与 ext 无关；mode 由 ext 是否非空决定",
    )
    ext: Dict[str, Any] = Field(
        default_factory=dict,
        description="右屏选择；空则 mode1 讨论，非空则 mode0。模板 id 放 selected_knowledge_ids[0]",
    )
    files_data: List[FileDataItem] = Field(
        default_factory=list,
        description="附件：name/type/content",
    )
    llm_model_config: Optional[Dict[str, Any]] = Field(
        default=None, validation_alias="model_config"
    )
    id: Optional[str] = Field(None, description="模型 ID（根级，可与 model_config 重复）")
    base_url: Optional[str] = Field(None, description="vLLM OpenAI 兼容 base_url")
    api_key: Optional[str] = Field(None, description="API Key，内网 vLLM 常为 EMPTY")

    user_id: Optional[int] = Field(None, description="用户 ID")
    agent_id: Optional[int] = Field(5, description="智能体 ID")
    agent_name: Optional[str] = Field("舆情事件", description="智能体名称")
    title: Optional[str] = Field(None, description="会话标题")

    event_id: Optional[str] = Field(None, description="事件 ID（v1 兼容）")
    event_type: Optional[str] = Field("analysis_request", description="事件类型")
    topic: Optional[str] = Field(None, description="分析主题（v1 兼容）")
    query: Optional[str] = Field(None, description="分析查询（v1 兼容）")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_content_source(self):
        from singleAgent.ext_utils import ext_has_case_material, ext_has_template_selection
        from singleAgent.template_selection import (
            TEMPLATE_GENERATION_STAGE,
            TEMPLATE_INTERACTION_STAGES,
            TEMPLATE_SELECTION_STAGE,
            resolve_interaction_stage,
        )

        has_instruction = bool((self.instruction or "").strip())
        has_topic = bool((self.topic or "").strip())
        has_ext = ext_has_case_material(self.ext if isinstance(self.ext, dict) else {})
        has_template = ext_has_template_selection(
            self.ext if isinstance(self.ext, dict) else {}
        )
        has_files = bool(self.files_data)
        stage = resolve_interaction_stage(self.metadata)
        if stage and stage not in TEMPLATE_INTERACTION_STAGES:
            raise ValueError(f"不支持的 interaction_stage: {stage}")
        if stage == TEMPLATE_SELECTION_STAGE:
            if not has_instruction:
                raise ValueError("template_selection 需要提供 instruction")
            if self.ismodel != 1:
                raise ValueError("template_selection 要求 ismodel=1")
            if self.ext:
                raise ValueError("template_selection 阶段 ext 必须为空")
            if has_files:
                raise ValueError("template_selection 阶段不支持附件")
        elif stage == TEMPLATE_GENERATION_STAGE:
            if not has_template:
                raise ValueError("template_generation 需要选择报告模板")
            if not (self.source_request_id or "").strip():
                raise ValueError("template_generation 需要 source_request_id")
            if has_files:
                raise ValueError("template_generation 阶段不支持附件")
        if not (has_instruction or has_topic or has_ext or has_template or has_files):
            raise ValueError(
                "需要提供 instruction、topic、ext（案例或已选模板）或 files_data（附件）至少一项"
            )
        for item in self.files_data:
            ft = _normalize_attachment_type(item.type)
            if ft and ft not in ALLOWED_ATTACHMENT_TYPES:
                raise ValueError(
                    f"不支持的附件格式「{item.type}」（文件 {item.name or '?'}），"
                    f"仅允许: docx, xlsx, pdf, png, jpg"
                )
        return self

    def resolve_llm(self) -> Dict[str, str]:
        nested = self.llm_model_config or {}
        api_key = nested.get("api_key") or self.api_key or ""
        base_url = nested.get("base_url") or self.base_url or ""
        model_name = nested.get("id") or self.id or ""
        return {
            "api_key": api_key,
            "base_url": base_url,
            "model_name": model_name,
        }

    def to_analysis_event(self) -> AnalysisEvent:
        instruction = (self.instruction or "").strip()
        explicit_topic = (self.topic or "").strip()
        explicit_query = (self.query or "").strip()

        if instruction or self.ext or self.files_data:
            topic = explicit_topic or _derive_topic_from_request(
                instruction, self.ext, self.files_data
            )
            query = explicit_query or _build_analysis_query(
                instruction, self.ext, self.files_data
            )
        else:
            topic = explicit_topic
            query = explicit_query or topic

        event_id = (
            (self.event_id or "").strip()
            or (self.session_id or "").strip()
            or str(uuid.uuid4())
        )

        session_id = (self.session_id or "").strip() or event_id
        request_id = (self.request_id or "").strip() or f"gen_{uuid.uuid4().hex}"

        ext_payload = self.ext if isinstance(self.ext, dict) else {}
        # ismodel 与 ext 无关：尊重前端传入（默认 2）
        ismodel = self.ismodel if self.ismodel in (1, 2) else 2

        metadata = dict(self.metadata or {})
        metadata.update({
            "session_id": session_id,
            "request_id": request_id,
            "source_request_id": (self.source_request_id or "").strip(),
            "turn_status": "submitted",
            "instruction": instruction,
            "ismodel": ismodel,
            "knowledge_base_ids": _normalize_knowledge_base_ids(self.knowledge_base_ids),
            "ext": ext_payload,
            "files_data": [item.model_dump() for item in self.files_data],
            "user_id": self.user_id,
            "agent_id": self.agent_id or 5,
            "agent_name": self.agent_name or "舆情事件",
            "title": self.title or topic,
        })
        llm = self.resolve_llm()
        if any(llm.values()):
            metadata["model_config"] = llm

        return AnalysisEvent(
            event_id=event_id,
            event_type=self.event_type or "analysis_request",
            topic=topic,
            query=query,
            metadata=metadata,
        )


class Event2StopRequest(BaseModel):
    """POST /api/v1/analysis/event2/stop — 前端只需传 session_id。"""

    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(..., description="会话 ID；停止该会话当前正在生成的一轮")


def _normalize_attachment_type(file_type: str) -> str:
    t = (file_type or "").strip().lower().lstrip(".")
    return "jpg" if t == "jpeg" else t


def _normalize_knowledge_base_ids(ids: List[Union[int, str]]) -> List[str]:
    from singleAgent.knowledge_base import normalize_knowledge_base_ids

    return normalize_knowledge_base_ids(ids)


def _short_title(text: str, limit: int = 80) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit] or "舆情事件分析"


def _mask_secret(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-4:]}"


def _extract_selected_template_id(ext: Dict[str, Any]) -> str:
    if not isinstance(ext, dict):
        return ""
    for key in ("report_template_id", "report_template", "template_id"):
        value = ext.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    selected = ext.get("selected_knowledge_ids")
    if isinstance(selected, list) and selected:
        return str(selected[0])
    if isinstance(selected, str) and selected.strip():
        return selected.strip()
    return ""


def _normalize_related_cases(ext: Dict[str, Any]) -> List[Dict[str, str]]:
    if not isinstance(ext, dict):
        return []
    raw = (
        ext.get("related_cases")
        or ext.get("selected_cases")
        or ext.get("cases")
        or ext.get("case_ids")
        or []
    )
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    cases: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            case_id = str(item.get("id") or item.get("case_id") or item.get("uuid") or "").strip()
            name = str(item.get("name") or item.get("title") or case_id).strip()
        else:
            case_id = str(item).strip()
            name = case_id
        if case_id:
            cases.append({"id": case_id, "name": name or case_id})
    return cases


def _event2_model_summary(llm: Dict[str, str]) -> Dict[str, str]:
    return {
        "id": llm.get("model_name") or llm.get("id") or "",
        "base_url": llm.get("base_url") or "",
        "api_key": _mask_secret(llm.get("api_key") or ""),
    }


def _build_event2_human_metadata(
    body: AnalysisEventRequest,
    analysis_event: AnalysisEvent,
    llm: Dict[str, str],
) -> Dict[str, Any]:
    files = [item.model_dump() for item in body.files_data]
    event_meta = analysis_event.metadata or {}
    return {
        "module_name": "舆情事件分析",
        "knowledge_base_ids": _normalize_knowledge_base_ids(body.knowledge_base_ids),
        "ragflow_top_k": int((body.metadata or {}).get("ragflow_top_k") or 5),
        # 前端历史回显：conversations.metadata.filesData
        "filesData": files,
        "files_data_count": len(files),
        "files_data_names": [item.get("name") or f"附件{i + 1}" for i, item in enumerate(files)],
        "related_cases": _normalize_related_cases(body.ext),
        "selected_template_id": _extract_selected_template_id(body.ext),
        "model_config_summary": _event2_model_summary(llm),
        "rag_reference_count": 0,
        "event_id": analysis_event.event_id,
        "request_id": event_meta.get("request_id") or "",
        "source_request_id": event_meta.get("source_request_id") or "",
        "interaction_stage": event_meta.get("interaction_stage") or "",
        "pending_template_request_id": event_meta.get("pending_template_request_id") or "",
        "turn_status": "submitted",
        "event_type": analysis_event.event_type,
        "topic": analysis_event.topic,
    }


def _event2_json_response(data: Any, msg: str = "查询成功", code: int = 0) -> JSONResponse:
    return JSONResponse(content={"code": code, "msg": msg, "data": data})


def _event2_cancelled_response(
    *,
    request: Request,
    session_store: Any,
    session_id: str,
    analysis_event: Any,
    cancellation_handle: Any,
):
    """Return the request-level cancellation contract from any entrypoint path."""
    request_id = cancellation_handle.request_id
    cancel_reason = cancellation_handle.cancel_reason or "user"
    if session_store is not None and analysis_event is not None:
        try:
            from singleAgent.pipeline import _persist_cancelled_turn

            cancelled_meta = dict(getattr(analysis_event, "metadata", {}) or {})
            cancelled_instruction = (
                cancelled_meta.get("instruction")
                or getattr(analysis_event, "query", "")
                or getattr(analysis_event, "topic", "")
                or ""
            ).strip()
            _persist_cancelled_turn(
                session_store,
                session_id,
                analysis_event,
                cancelled_instruction,
                cancelled_meta,
                request_id,
                cancellation_handle,
                reason=cancel_reason,
                fallback_content="已停止输出。",
                reference_done=True,
            )
        except Exception as persist_error:
            _log_to_file(f"[event2] 写入取消会话失败: {persist_error}", "warning")

    accept_header = (request.headers.get("accept") or "application/json").lower()
    if "text/event-stream" in accept_header:
        def cancelled_generator():
            from singleAgent.sse import (
                attach_event_identity,
                emit_cancelled,
                emit_reference_done,
            )

            yield attach_event_identity(
                emit_cancelled(
                    request_id=request_id,
                    session_id=session_id,
                    reason=cancel_reason,
                ),
                request_id=request_id,
                session_id=session_id,
            )
            yield attach_event_identity(
                emit_reference_done(),
                request_id=request_id,
                session_id=session_id,
            )

        return StreamingResponse(
            cancelled_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
            },
        )

    return _event2_json_response(
        {
            "request_id": request_id,
            "session_id": session_id,
            "cancelled": True,
            "reason": cancel_reason,
        },
        msg="cancelled",
    )


def _get_event2_session_store():
    from singleAgent.session_store import get_session_store

    return get_session_store(PROJECT_ROOT)


def _format_ext_content(ext: Dict[str, Any]) -> str:
    """将案例 ext 转为可拼入 prompt 的文本（不含纯模板选择字段）。"""
    from singleAgent.ext_utils import ext_has_case_material

    if not ext_has_case_material(ext):
        return ""
    for key in (
        "content",
        "text",
        "description",
        "case_content",
        "brief",
        "instruction",
        "title",
    ):
        val = ext.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        return json.dumps(ext, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(ext)


def _format_files_data(files: List[FileDataItem]) -> str:
    blocks: List[str] = []
    for i, item in enumerate(files, 1):
        name = (item.name or f"附件{i}").strip()
        ft = _normalize_attachment_type(item.type) or "unknown"
        content = (item.content or "").strip()
        if not content:
            content = "（附件无文本内容）"
        blocks.append(f"### 附件 {i}: {name}（{ft}）\n\n{content}")
    return "\n\n".join(blocks)


def _build_analysis_query(
    instruction: str,
    ext: Dict[str, Any],
    files: List[FileDataItem],
) -> str:
    """合并用户输入、案例与附件，供三引擎与 ReportEngine 使用。"""
    parts: List[str] = []
    if instruction:
        parts.append(f"【用户输入】\n{instruction}")
    ext_text = _format_ext_content(ext)
    if ext_text:
        parts.append(f"【所选案例】\n{ext_text}")
    if files:
        parts.append(f"【附件材料】\n{_format_files_data(files)}")
    return "\n\n".join(parts)


def _derive_topic_from_request(
    instruction: str,
    ext: Dict[str, Any],
    files: List[FileDataItem],
    max_len: int = 80,
) -> str:
    if instruction:
        return _derive_topic_from_instruction(instruction, max_len=max_len)
    if isinstance(ext, dict):
        for key in ("title", "name", "case_name", "topic", "label", "brief"):
            val = ext.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                return text[:max_len] + ("…" if len(text) > max_len else "")
    if files:
        first_name = (files[0].name or "").strip()
        if first_name:
            stem = Path(first_name).stem
            if stem:
                return stem[:max_len]
    return "未命名主题"


def _derive_topic_from_instruction(instruction: str, max_len: int = 80) -> str:
    """从长 instruction 截取短主题（首句或前 max_len 字）。"""
    text = " ".join(instruction.strip().split())
    if not text:
        return "未命名主题"
    for sep in ("。", "！", "？", ".", "!", "?"):
        idx = text.find(sep)
        if 0 < idx <= max_len:
            return text[: idx + 1].strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def _apply_llm_overrides(config: Dict[str, Any], llm: Dict[str, str]) -> Dict[str, Any]:
    """将前端 model_config 覆盖到各引擎（及 Keyword Optimizer）配置。"""
    api_key = llm.get("api_key") or ""
    base_url = llm.get("base_url") or ""
    model_name = llm.get("model_name") or ""
    if not any([api_key, base_url, model_name]):
        return config

    cfg = dict(config)
    for prefix in (
        "INSIGHT_ENGINE",
        "MEDIA_ENGINE",
        "QUERY_ENGINE",
        "REPORT_ENGINE",
        "KEYWORD_OPTIMIZER",
    ):
        if api_key:
            cfg[f"{prefix}_API_KEY"] = api_key
        if base_url:
            cfg[f"{prefix}_BASE_URL"] = base_url
        if model_name:
            cfg[f"{prefix}_MODEL_NAME"] = model_name
    return cfg

class AnalysisResponse(BaseModel):
    """分析响应模型"""
    success: bool
    event_id: str
    task_id: Optional[str] = None
    message: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    data: Optional[Dict[str, Any]] = None

class EngineStatus(BaseModel):
    """引擎状态模型"""
    engine: str
    status: str  # pending, running, completed, error, skipped
    progress: int = 0
    message: str = ""

class ReportStatus(BaseModel):
    """报告状态模型"""
    task_id: str
    status: str  # pending, running, completed, error
    progress: int = 0
    engines: List[EngineStatus] = []
    report_file: Optional[str] = None
    ir_file_path: Optional[str] = None
    pdf_file: Optional[str] = None
    pdf_ready: bool = False
    pdf_error: Optional[str] = None
    markdown_file: Optional[str] = None
    markdown_ready: bool = False
    markdown_error: Optional[str] = None
    markdown_url: Optional[str] = None
    error_reason: Optional[str] = None
    error_message: Optional[str] = None

class HealthCheckResponse(BaseModel):
    """健康检查响应"""
    status: str
    mock_search_api: str
    dependencies: Dict[str, str]
    timestamp: str

# ==================== 枚举定义 ====================

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"

class EngineType(str, Enum):
    INSIGHT = "insight"
    MEDIA = "media"
    QUERY = "query"
    FORUM = "forum"
    REPORT = "report"

# ==================== 工具函数 ====================

def _markdown_url_for(task_id: str) -> str:
    return f"/api/v1/analysis/report/{task_id}/markdown"


def _parse_sse_format(request: Optional[Request], metadata: Optional[Dict[str, Any]]) -> str:
    """返回 legacy（默认）或 decision（辅助决策前端协议）。"""
    if request is not None:
        hdr = (request.headers.get("X-SSE-Format") or "").strip().lower()
        if hdr in ("decision", "assist", "frontend"):
            return "decision"
        qp = request.query_params.get("sse_format")
        if qp and str(qp).strip().lower() in ("decision", "assist", "frontend"):
            return "decision"
    if metadata:
        fmt = metadata.get("sse_format") or (metadata.get("ext") or {}).get("sse_format")
        if fmt and str(fmt).strip().lower() in ("decision", "assist", "frontend"):
            return "decision"
    return "legacy"


def _parse_stream_verbose(request: Optional[Request], metadata: Optional[Dict[str, Any]]) -> bool:
    if request is not None:
        hdr = (request.headers.get("X-Stream-Verbose") or "").strip().lower()
        if hdr in ("1", "true", "yes"):
            return True
        qp = request.query_params.get("verbose")
        if qp and str(qp).strip().lower() in ("1", "true", "yes"):
            return True
    if metadata and metadata.get("verbose_stream"):
        return bool(metadata.get("verbose_stream"))
    return False


def _analysis_sse_generator(
    task: "AnalysisTask",
    last_event_id: Optional[int] = None,
    emit_task_created: bool = False,
):
    """共享 SSE 生成器：历史回放 + 实时订阅 + 心跳。"""
    if emit_task_created:
        created = {
            "id": 0,
            "type": "task_created",
            "task_id": task.task_id,
            "timestamp": datetime.now().isoformat(),
            "payload": {
                "task_id": task.task_id,
                "event_id": task.event_id,
                "session_id": task.session_id,
                "topic": task.topic,
                "query": task.query,
                "resume_stream_url": f"/api/v1/analysis/stream/{task.task_id}",
                "markdown_url": _markdown_url_for(task.task_id),
            },
        }
        yield TaskEventBus.format_sse(created)

    queue = task.event_bus.subscribe()
    try:
        for event in task.event_bus.history_since(last_event_id):
            yield TaskEventBus.format_sse(event)
            if event.get("type") in STREAM_TERMINAL_TYPES:
                return

        terminal_statuses = {
            TaskStatus.COMPLETED,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }

        while True:
            if task.status in terminal_statuses:
                try:
                    while True:
                        event = queue.get_nowait()
                        yield TaskEventBus.format_sse(event)
                        if event.get("type") in STREAM_TERMINAL_TYPES:
                            return
                except Empty:
                    pass
                break

            try:
                event = queue.get(timeout=STREAM_HEARTBEAT_INTERVAL)
            except Empty:
                yield TaskEventBus.format_sse(
                    TaskEventBus.heartbeat(task.task_id, task.status.value)
                )
                if task.status in terminal_statuses:
                    break
                continue

            yield TaskEventBus.format_sse(event)
            if event.get("type") in STREAM_TERMINAL_TYPES:
                break
    finally:
        task.event_bus.unsubscribe(queue)


def _decision_sse_generator(
    task: "AnalysisTask",
    last_event_id: Optional[int] = None,
):
    """辅助决策前端 SSE：thinking（CoT）+ result（Markdown 流）。"""
    ctx = DecisionStreamContext(title=task.topic or "分析报告")
    translator = DecisionSSETranslator(ctx)

    if last_event_id is None:
        yield format_decision_sse(
            "thinking",
            {"type": "progress", "message": "收到请求..."},
        )

    queue = task.event_bus.subscribe()
    try:
        for event in task.event_bus.history_since(last_event_id):
            for line in translator.translate(event):
                yield line
            if event.get("type") in STREAM_TERMINAL_TYPES:
                return

        terminal_statuses = {
            TaskStatus.COMPLETED,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }

        while True:
            if task.status in terminal_statuses:
                try:
                    while True:
                        event = queue.get_nowait()
                        for line in translator.translate(event):
                            yield line
                        if event.get("type") in STREAM_TERMINAL_TYPES:
                            return
                except Empty:
                    pass
                break

            try:
                event = queue.get(timeout=STREAM_HEARTBEAT_INTERVAL)
            except Empty:
                yield format_decision_ping()
                if task.status in terminal_statuses:
                    break
                continue

            for line in translator.translate(event):
                yield line
            if event.get("type") in STREAM_TERMINAL_TYPES:
                break
    finally:
        task.event_bus.unsubscribe(queue)


def _sse_generator_for_task(
    task: "AnalysisTask",
    last_event_id: Optional[int] = None,
    emit_task_created: bool = False,
):
    if getattr(task, "sse_format", "legacy") == "decision":
        return _decision_sse_generator(task, last_event_id=last_event_id)
    return _analysis_sse_generator(
        task,
        last_event_id=last_event_id,
        emit_task_created=emit_task_created,
    )


def _log_to_file(message: str, log_type: str = "api"):
    """写入日志文件"""
    try:
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{log_type}.log"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception as e:
        print(f"写入日志失败: {e}")

# Global cache for mock_search_api health status
_health_cache = {
    "available": False,
    "last_check": None
}
_module_health_cache = {
    "status": None,
    "last_check": None,
}


def check_mock_search_api() -> bool:
    """
    检查mock_search_api服务是否可用（使用缓存，避免每次健康检查都发送请求）
    缓存有效期：30秒
    """
    from datetime import datetime

    # 检查缓存：如果最近30秒内检查过，直接返回缓存结果
    if _health_cache["last_check"]:
        elapsed = (datetime.now() - _health_cache["last_check"]).total_seconds()
        if elapsed < 30:
            logger.debug(f"使用缓存的mock_search_api状态: {_health_cache['available']} (缓存于{elapsed:.1f}秒前)")
            return _health_cache["available"]

    try:
        import requests
        from requests.adapters import HTTPAdapter
        # Docker 探活只有数秒；绝不能用超长 timeout 堵事件循环
        mock_timeout = float(os.getenv("HEALTH_MOCK_TIMEOUT_SEC", "2") or "2")
        logger.info(
            f"正在检查 mock_search_api: "
            f"http://{MOCK_SEARCH_API_HOST}:{MOCK_SEARCH_API_PORT}/health "
            f"timeout={mock_timeout}s"
        )

        # 创建会话并强制使用 HTTP 适配器
        session = requests.Session()
        session.mount('http://', HTTPAdapter())
        session.mount('https://', HTTPAdapter())

        response = session.get(
            f"http://{MOCK_SEARCH_API_HOST}:{MOCK_SEARCH_API_PORT}/health",
            timeout=mock_timeout,
            proxies={'http': None, 'https': None}  # 禁用代理
        )
        logger.info(f"mock_search_api 响应: status_code={response.status_code}")
        # 接受 200（健康）或 503（不健康但服务在运行）都视为可用
        is_available = response.status_code in [200, 503]
        logger.info(f"mock_search_api 可用性: {is_available}")

        # 更新缓存
        _health_cache["available"] = is_available
        _health_cache["last_check"] = datetime.now()

        return is_available
    except Exception as e:
        from singleAgent.diag_codes import E2202_HEALTH_MOCK_FAIL, format_diag

        logger.error(
            format_diag(
                E2202_HEALTH_MOCK_FAIL,
                "mock_probe_failed",
                err_type=type(e).__name__,
            )
        )
        _health_cache["available"] = False
        _health_cache["last_check"] = datetime.now()
        return False

# ==================== 引擎执行器类 ====================

@dataclass
class EngineExecutor:
    """单个引擎的执行器"""
    engine_type: EngineType
    query: str
    config: Dict[str, Any]
    progress_callback: Optional[callable] = None
    
    def __post_init__(self):
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.message = ""
        self.error = None
        self.output_file = None
        self.thread = None
        
    def update_progress(self, progress: int, message: str):
        """更新进度"""
        self.progress = progress
        self.message = message
        if self.progress_callback:
            self.progress_callback(self.engine_type.value, progress, message)
        _log_to_file(f"[{self.engine_type.value}] {progress}% - {message}")
    
    def get_output_file(self) -> Optional[str]:
        """获取最新的输出文件"""
        engine_dir = ENGINE_OUTPUT_DIRS.get(self.engine_type.value)
        if not engine_dir or not os.path.exists(engine_dir):
            return None
        
        md_files = [f for f in os.listdir(engine_dir) if f.endswith('.md')]
        if not md_files:
            return None
        
        # 返回最新的md文件
        latest_file = max(md_files, key=lambda x: os.path.getmtime(os.path.join(engine_dir, x)))
        return os.path.join(engine_dir, latest_file)


class InsightEngineExecutor(EngineExecutor):
    """InsightEngine执行器"""
    
    def __init__(self, query: str, config: Dict[str, Any], progress_callback: Optional[callable] = None):
        super().__init__(EngineType.INSIGHT, query, config, progress_callback)
    
    def run(self):
        """执行InsightEngine分析"""
        try:
            self.status = TaskStatus.RUNNING
            self.update_progress(0, "正在初始化InsightEngine...")
            
            # 导入InsightEngine模块
            from InsightEngine import DeepSearchAgent
            from InsightEngine.utils.config import Settings
            
            # 创建设置对象
            settings_obj = Settings(
                INSIGHT_ENGINE_API_KEY=self.config.get('INSIGHT_ENGINE_API_KEY'),
                INSIGHT_ENGINE_BASE_URL=self.config.get('INSIGHT_ENGINE_BASE_URL'),
                INSIGHT_ENGINE_MODEL_NAME=self.config.get('INSIGHT_ENGINE_MODEL_NAME'),
                DB_HOST=self.config.get('DB_HOST'),
                DB_USER=self.config.get('DB_USER'),
                DB_PASSWORD=self.config.get('DB_PASSWORD'),
                DB_NAME=self.config.get('DB_NAME'),
                DB_PORT=self.config.get('DB_PORT'),
                DB_CHARSET=self.config.get('DB_CHARSET'),
                DB_DIALECT=self.config.get('DB_DIALECT'),
                MAX_REFLECTIONS=2,
                MAX_CONTENT_LENGTH=500000,
                OUTPUT_DIR=ENGINE_OUTPUT_DIRS['insight']
            )
            
            # 初始化Agent
            self.update_progress(5, "正在创建DeepSearchAgent...")
            agent = DeepSearchAgent(settings_obj)
            
            # 生成报告结构
            self.update_progress(10, "正在生成报告结构...")
            agent._generate_report_structure(self.query)
            
            # 处理段落
            total_paragraphs = len(agent.state.paragraphs)
            for i in range(total_paragraphs):
                self.update_progress(
                    10 + int(80 * (i + 1) / total_paragraphs),
                    f"正在处理段落 {i + 1}/{total_paragraphs}: {agent.state.paragraphs[i].title}"
                )
                
                # 初始搜索和总结
                agent._initial_search_and_summary(i)
                
                # 反思循环
                agent._reflection_loop(i)
                agent.state.paragraphs[i].research.mark_completed()
            
            # 生成最终报告
            self.update_progress(90, "正在生成最终报告...")
            final_report = agent._generate_final_report()
            
            # 保存报告
            self.update_progress(95, "正在保存报告...")
            agent._save_report(final_report)
            
            # 获取输出文件
            self.output_file = self.get_output_file()
            
            self.update_progress(100, "InsightEngine分析完成")
            self.status = TaskStatus.COMPLETED
            _log_to_file(f"InsightEngine完成，输出文件: {self.output_file}")
            
        except Exception as e:
            self.error = str(e)
            self.status = TaskStatus.ERROR
            self.update_progress(0, f"InsightEngine执行失败: {e}")
            _log_to_file(f"InsightEngine执行失败: {e}", "error")
    
    def start(self):
        """在新线程中运行"""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()


class MediaEngineExecutor(EngineExecutor):
    """MediaEngine执行器"""
    
    def __init__(self, query: str, config: Dict[str, Any], progress_callback: Optional[callable] = None):
        super().__init__(EngineType.MEDIA, query, config, progress_callback)
    
    def run(self):
        """执行MediaEngine分析"""
        try:
            self.status = TaskStatus.RUNNING
            self.update_progress(0, "正在初始化MediaEngine...")
            
            # 导入MediaEngine模块
            from MediaEngine import DeepSearchAgent, AnspireSearchAgent
            from MediaEngine.utils.config import Settings
            
            # 创建设置对象
            search_tool_type = self.config.get('SEARCH_TOOL_TYPE', 'BochaAPI')
            
            if search_tool_type == 'BochaAPI':
                settings_obj = Settings(
                    MEDIA_ENGINE_API_KEY=self.config.get('MEDIA_ENGINE_API_KEY'),
                    MEDIA_ENGINE_BASE_URL=self.config.get('MEDIA_ENGINE_BASE_URL'),
                    MEDIA_ENGINE_MODEL_NAME=self.config.get('MEDIA_ENGINE_MODEL_NAME'),
                    SEARCH_TOOL_TYPE='BochaAPI',
                    BOCHA_WEB_SEARCH_API_KEY=self.config.get('BOCHA_WEB_SEARCH_API_KEY'),
                    MAX_REFLECTIONS=2,
                    SEARCH_CONTENT_MAX_LENGTH=20000,
                    OUTPUT_DIR=ENGINE_OUTPUT_DIRS['media']
                )
                agent = DeepSearchAgent(settings_obj)
            else:
                settings_obj = Settings(
                    MEDIA_ENGINE_API_KEY=self.config.get('MEDIA_ENGINE_API_KEY'),
                    MEDIA_ENGINE_BASE_URL=self.config.get('MEDIA_ENGINE_BASE_URL'),
                    MEDIA_ENGINE_MODEL_NAME=self.config.get('MEDIA_ENGINE_MODEL_NAME'),
                    SEARCH_TOOL_TYPE='AnspireAPI',
                    ANSPIRE_API_KEY=self.config.get('ANSPIRE_API_KEY'),
                    MAX_REFLECTIONS=2,
                    SEARCH_CONTENT_MAX_LENGTH=20000,
                    OUTPUT_DIR=ENGINE_OUTPUT_DIRS['media']
                )
                agent = AnspireSearchAgent(settings_obj)
            
            # 生成报告结构
            self.update_progress(10, "正在生成报告结构...")
            agent._generate_report_structure(self.query)
            
            # 处理段落
            total_paragraphs = len(agent.state.paragraphs)
            for i in range(total_paragraphs):
                self.update_progress(
                    10 + int(80 * (i + 1) / total_paragraphs),
                    f"正在处理段落 {i + 1}/{total_paragraphs}: {agent.state.paragraphs[i].title}"
                )
                
                # 初始搜索和总结
                agent._initial_search_and_summary(i)
                
                # 反思循环
                agent._reflection_loop(i)
                agent.state.paragraphs[i].research.mark_completed()
            
            # 生成最终报告
            self.update_progress(90, "正在生成最终报告...")
            final_report = agent._generate_final_report()
            
            # 保存报告
            self.update_progress(95, "正在保存报告...")
            agent._save_report(final_report)
            
            # 获取输出文件
            self.output_file = self.get_output_file()
            
            self.update_progress(100, "MediaEngine分析完成")
            self.status = TaskStatus.COMPLETED
            _log_to_file(f"MediaEngine完成，输出文件: {self.output_file}")
            
        except Exception as e:
            self.error = str(e)
            self.status = TaskStatus.ERROR
            self.update_progress(0, f"MediaEngine执行失败: {e}")
            _log_to_file(f"MediaEngine执行失败: {e}", "error")
    
    def start(self):
        """在新线程中运行"""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()


class QueryEngineExecutor(EngineExecutor):
    """QueryEngine执行器"""
    
    def __init__(self, query: str, config: Dict[str, Any], progress_callback: Optional[callable] = None):
        super().__init__(EngineType.QUERY, query, config, progress_callback)
    
    def run(self):
        """执行QueryEngine分析"""
        try:
            self.status = TaskStatus.RUNNING
            self.update_progress(0, "正在初始化QueryEngine...")
            
            # 导入QueryEngine模块
            from QueryEngine import DeepSearchAgent
            from QueryEngine.utils.config import Settings
            
            # 创建设置对象
            settings_obj = Settings(
                QUERY_ENGINE_API_KEY=self.config.get('QUERY_ENGINE_API_KEY'),
                QUERY_ENGINE_BASE_URL=self.config.get('QUERY_ENGINE_BASE_URL'),
                QUERY_ENGINE_MODEL_NAME=self.config.get('QUERY_ENGINE_MODEL_NAME'),
                TAVILY_API_KEY=self.config.get('TAVILY_API_KEY'),
                MAX_REFLECTIONS=2,
                SEARCH_CONTENT_MAX_LENGTH=20000,
                OUTPUT_DIR=ENGINE_OUTPUT_DIRS['query']
            )
            
            # 初始化Agent
            self.update_progress(5, "正在创建DeepSearchAgent...")
            agent = DeepSearchAgent(settings_obj)
            
            # 生成报告结构
            self.update_progress(10, "正在生成报告结构...")
            agent._generate_report_structure(self.query)
            
            # 处理段落
            total_paragraphs = len(agent.state.paragraphs)
            for i in range(total_paragraphs):
                self.update_progress(
                    10 + int(80 * (i + 1) / total_paragraphs),
                    f"正在处理段落 {i + 1}/{total_paragraphs}: {agent.state.paragraphs[i].title}"
                )
                
                # 初始搜索和总结
                agent._initial_search_and_summary(i)
                
                # 反思循环
                agent._reflection_loop(i)
                agent.state.paragraphs[i].research.mark_completed()
            
            # 生成最终报告
            self.update_progress(90, "正在生成最终报告...")
            final_report = agent._generate_final_report()
            
            # 保存报告
            self.update_progress(95, "正在保存报告...")
            agent._save_report(final_report)
            
            # 获取输出文件
            self.output_file = self.get_output_file()
            
            self.update_progress(100, "QueryEngine分析完成")
            self.status = TaskStatus.COMPLETED
            _log_to_file(f"QueryEngine完成，输出文件: {self.output_file}")
            
        except Exception as e:
            self.error = str(e)
            self.status = TaskStatus.ERROR
            self.update_progress(0, f"QueryEngine执行失败: {e}")
            _log_to_file(f"QueryEngine执行失败: {e}", "error")
    
    def start(self):
        """在新线程中运行"""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()


class ForumEngineExecutor:
    """ForumEngine日志监控执行器"""
    
    def __init__(self, progress_callback: Optional[callable] = None):
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.message = ""
        self.error = None
        self.thread = None
        self.monitor = None
        self.progress_callback = progress_callback
    
    def update_progress(self, progress: int, message: str):
        """更新进度"""
        self.progress = progress
        self.message = message
        if self.progress_callback:
            self.progress_callback(EngineType.FORUM.value, progress, message)
        _log_to_file(f"[forum] {progress}% - {message}")
    
    def run(self):
        """执行ForumEngine监控"""
        try:
            self.status = TaskStatus.RUNNING
            self.update_progress(0, "正在启动ForumEngine监控...")
            
            # 导入ForumEngine模块
            from ForumEngine.monitor import LogMonitor
            
            # 清空并初始化forum.log
            log_dir = PROJECT_ROOT / "logs"
            log_dir.mkdir(exist_ok=True)
            forum_log_file = log_dir / "forum.log"
            
            # 清空forum.log
            with open(forum_log_file, 'w', encoding='utf-8') as f:
                f.write(f"=== ForumEngine 监控开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            
            # 创建并启动日志监控器
            self.monitor = LogMonitor(log_dir=str(log_dir))
            self.monitor.start_monitoring()
            
            self.update_progress(100, "ForumEngine监控已启动")
            _log_to_file("ForumEngine日志监控已启动")
            
            # ForumEngine会持续运行，不需要标记为完成
            
        except Exception as e:
            self.error = str(e)
            self.status = TaskStatus.ERROR
            self.update_progress(0, f"ForumEngine启动失败: {e}")
            _log_to_file(f"ForumEngine启动失败: {e}", "error")
    
    def start(self):
        """在新线程中运行"""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
    
    def stop(self):
        """停止ForumEngine监控"""
        if self.monitor:
            try:
                self.monitor.stop_monitoring()
            except Exception as e:
                _log_to_file(f"停止ForumEngine监控失败: {e}", "error")


# ==================== 报告生成器 ====================

class ReportGenerator:
    """综合报告生成器"""
    
    def __init__(
        self,
        engines_output: Dict[str, str],
        query: str,
        progress_callback: Optional[callable] = None,
        event_bus: Optional[TaskEventBus] = None,
        report_event_emitter: Optional[callable] = None,
        stream_markdown: bool = False,
    ):
        self.engines_output = engines_output
        self.query = query
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.message = ""
        self.error = None
        self.report_file = None
        self.html_content = None
        self.ir_file_path = None
        self.pdf_file = None
        self.pdf_ready = False
        self.pdf_error = None
        self.markdown_file = None
        self.markdown_ready = False
        self.markdown_error = None
        self.progress_callback = progress_callback
        self.event_bus = event_bus
        self.report_event_emitter = report_event_emitter
        self.stream_markdown = stream_markdown
    
    def _resolve_path(self, file_path: str) -> Path:
        """将相对路径解析为绝对路径。"""
        path = Path(file_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    
    def _emit_report_event(self, payload: Dict[str, Any]) -> None:
        """转发 ReportEngine 流式事件（经任务层过滤 delta 并映射 workflow）。"""
        if self.report_event_emitter:
            self.report_event_emitter(payload)
        elif self.event_bus:
            report_payload: Dict[str, Any] = {"report_event": payload.get("report_event")}
            report_payload.update(payload)
            self.event_bus.emit("report_stage", report_payload)

    def _generate_markdown(self, ir_filepath: str) -> None:
        """报告完成后从 IR 预生成 Markdown 并落盘（默认交付物）。"""
        try:
            self.update_progress(88, "正在生成 Markdown...")
            with open(ir_filepath, "r", encoding="utf-8") as f:
                document_ir = json.load(f)

            from ReportEngine.renderers import MarkdownRenderer
            renderer = MarkdownRenderer()
            markdown_text = renderer.render(document_ir, ir_file_path=ir_filepath)

            metadata = document_ir.get("metadata") if isinstance(document_ir, dict) else {}
            topic = (
                (metadata or {}).get("topic")
                or (metadata or {}).get("title")
                or (metadata or {}).get("query")
                or self.query
            )
            query_safe = "".join(
                c for c in (topic or self.query) if c.isalnum() or c in (" ", "-", "_")
            ).rstrip()
            query_safe = query_safe.replace(" ", "_")[:30] or "report"

            md_dir = PROJECT_ROOT / "final_reports"
            md_dir.mkdir(parents=True, exist_ok=True)
            md_filename = f"report_{query_safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            md_path = md_dir / md_filename
            md_path.write_text(markdown_text, encoding="utf-8")

            self.markdown_file = os.path.relpath(str(md_path.resolve()), os.getcwd())
            self.markdown_ready = True
            self.markdown_error = None
            _log_to_file(f"Markdown 已保存: {self.markdown_file}")
            self._emit_report_event({
                "report_event": "stage",
                "stage": "markdown_generated",
                "markdown_file": self.markdown_file,
            })
            if self.stream_markdown:
                self._stream_markdown_to_frontend(markdown_text, topic or self.query)
        except Exception as e:
            self.markdown_ready = False
            self.markdown_error = str(e)
            _log_to_file(f"Markdown 生成失败: {e}", "error")
            self._emit_report_event({
                "report_event": "error",
                "stage": "markdown_failed",
                "message": str(e),
            })

    def _stream_markdown_to_frontend(self, markdown_text: str, title: str) -> None:
        """将 Markdown 正文以 result/chunk 流式推送给前端（decision SSE）。"""
        if not self.report_event_emitter or not markdown_text:
            return
        for evt in build_markdown_stream_events(
            markdown_text,
            self.markdown_file or "",
            chunk_size=MARKDOWN_STREAM_CHUNK_SIZE,
            title=title or "分析报告",
        ):
            self._emit_report_event(evt)

    def _generate_pdf(self, ir_filepath: str) -> None:
        """报告完成后从 IR 预生成 PDF 并落盘（方案 B）。"""
        from ReportEngine.utils.dependency_check import check_pango_available

        pango_available, pango_message = check_pango_available()
        if not pango_available:
            self.pdf_ready = False
            self.pdf_error = pango_message or "PDF 导出功能不可用：缺少系统依赖"
            _log_to_file(f"PDF 跳过: {self.pdf_error}", "error")
            self._emit_report_event({
                "report_event": "stage",
                "stage": "pdf_skipped",
                "reason": self.pdf_error,
            })
            return

        try:
            self.update_progress(92, "正在生成PDF...")
            self._emit_report_event({
                "report_event": "stage",
                "stage": "pdf_generating",
            })

            with open(ir_filepath, "r", encoding="utf-8") as f:
                document_ir = json.load(f)

            from ReportEngine.renderers import PDFRenderer
            renderer = PDFRenderer()

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            query_safe = "".join(
                c for c in self.query if c.isalnum() or c in (" ", "-", "_")
            ).rstrip()
            query_safe = query_safe.replace(" ", "_")[:30] or "report"

            pdf_dir = PROJECT_ROOT / "final_reports" / "pdf"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_filename = f"final_report_{query_safe}_{timestamp}.pdf"
            pdf_path = pdf_dir / pdf_filename

            renderer.render_to_pdf(
                document_ir,
                pdf_path,
                optimize_layout=True,
                ir_file_path=ir_filepath,
            )

            self.pdf_file = os.path.relpath(str(pdf_path.resolve()), os.getcwd())
            self.pdf_ready = True
            self.pdf_error = None
            _log_to_file(f"PDF 已保存: {self.pdf_file}")
            self._emit_report_event({
                "report_event": "stage",
                "stage": "pdf_generated",
                "pdf_file": self.pdf_file,
            })
        except Exception as e:
            self.pdf_ready = False
            self.pdf_error = str(e)
            _log_to_file(f"PDF生成失败: {e}", "error")
            self._emit_report_event({
                "report_event": "error",
                "stage": "pdf_failed",
                "message": str(e),
            })
    
    def update_progress(self, progress: int, message: str):
        """更新进度"""
        self.progress = progress
        self.message = message
        if self.progress_callback:
            self.progress_callback(EngineType.REPORT.value, progress, message)
        _log_to_file(f"[report] {progress}% - {message}")
    
    def check_engines_ready(self) -> bool:
        """检查三个引擎的输出是否准备好"""
        missing = []
        for engine, file_path in self.engines_output.items():
            if not file_path or not os.path.exists(file_path):
                missing.append(engine)
        
        if missing:
            _log_to_file(f"以下引擎输出缺失: {missing}", "error")
            return False
        return True
    
    def run(self):
        """执行报告生成"""
        try:
            self.status = TaskStatus.RUNNING
            self.update_progress(0, "正在初始化ReportEngine...")
            
            # 检查引擎输出是否准备好
            self.update_progress(5, "正在检查引擎输出...")
            if not self.check_engines_ready():
                self.error = "部分引擎输出缺失"
                self.status = TaskStatus.ERROR
                self.update_progress(0, f"报告生成失败: {self.error}")
                return
            
            # 导入ReportEngine模块
            from ReportEngine.agent import create_agent
            import ReportEngine.flask_interface as report_api
            
            # 初始化ReportAgent
            self.update_progress(10, "正在创建ReportAgent...")
            report_agent = create_agent()
            # 关键修复：将同一实例注入 flask_interface 的全局上下文，
            # 避免 run_report_generation 使用未初始化的全局 report_agent。
            report_api.report_agent = report_agent
            
            # 准备输入文件
            latest_files = {}
            for engine, dir_name in ENGINE_OUTPUT_DIRS.items():
                if os.path.exists(dir_name):
                    md_files = [f for f in os.listdir(dir_name) if f.endswith('.md')]
                    if md_files:
                        latest_file = max(md_files, key=lambda x: os.path.getmtime(os.path.join(dir_name, x)))
                        latest_files[engine] = os.path.join(dir_name, latest_file)
            
            # 添加forum.log
            forum_log_path = os.path.join(PROJECT_ROOT, "logs", "forum.log")
            if os.path.exists(forum_log_path):
                latest_files['forum'] = forum_log_path
            
            self.update_progress(20, "正在加载引擎输出...")
            
            # 加载输入文件
            content = report_agent.load_input_files(latest_files)
            
            # 直接基于当前已加载内容生成报告（避免 run_report_generation 的新增文件计数检查误判）
            self.update_progress(30, "正在准备报告生成...")

            def stream_handler(event_type: str, payload: Dict[str, Any]):
                """转发 ReportEngine 流式进度到任务进度与 SSE。"""
                report_payload: Dict[str, Any] = {"report_event": event_type}
                if isinstance(payload, dict):
                    report_payload.update(payload)
                self._emit_report_event(report_payload)
                if event_type == "progress" and isinstance(payload, dict):
                    p = payload.get("progress")
                    if isinstance(p, (int, float)):
                        p = int(max(0, min(100, p)))
                        mapped = 30 + int(60 * p / 100)
                        if mapped > self.progress:
                            self.update_progress(mapped, f"报告生成中: {p}%")

            generation_result = report_agent.generate_report(
                query=self.query,
                reports=content.get("reports", []),
                forum_logs=content.get("forum_logs", ""),
                custom_template="",
                save_report=True,
                stream_handler=stream_handler,
                report_id=f"fastapi_{int(time.time())}",
            )

            if isinstance(generation_result, dict):
                self.report_file = generation_result.get("report_relative_path") or generation_result.get("report_filepath")
                self.html_content = generation_result.get("html_content")
                self.ir_file_path = generation_result.get("ir_relative_path") or generation_result.get("ir_filepath")
            else:
                self.html_content = generation_result
                self.report_file = None
                self.ir_file_path = None

            self.update_progress(90, "HTML报告生成完成，正在导出 Markdown...")

            if self.ir_file_path:
                ir_abs = str(self._resolve_path(self.ir_file_path))
                if os.path.exists(ir_abs):
                    self._generate_markdown(ir_abs)
                    if ENABLE_PDF_EXPORT:
                        self._generate_pdf(ir_abs)
                    else:
                        self.pdf_ready = False
                        self.pdf_error = None
                        self._emit_report_event({
                            "report_event": "stage",
                            "stage": "pdf_skipped",
                            "reason": "ENABLE_PDF_EXPORT 未开启",
                        })
                else:
                    self.markdown_ready = False
                    self.markdown_error = f"IR文件不存在: {self.ir_file_path}"
                    self.pdf_ready = False
                    self.pdf_error = self.markdown_error
                    _log_to_file(self.markdown_error, "error")
            else:
                self.markdown_ready = False
                self.markdown_error = "未找到IR文件路径，无法生成 Markdown"
                self.pdf_ready = False
                self.pdf_error = self.markdown_error
                _log_to_file(self.markdown_error, "error")

            if self.status != TaskStatus.ERROR:
                if self.markdown_ready:
                    completion_msg = "报告与 Markdown 已生成"
                    if ENABLE_PDF_EXPORT and self.pdf_ready:
                        completion_msg = "报告、Markdown 与 PDF 已生成"
                    elif ENABLE_PDF_EXPORT:
                        completion_msg = "报告与 Markdown 已生成（PDF 未就绪）"
                else:
                    completion_msg = "HTML 报告已生成（Markdown 未就绪）"
                self.update_progress(100, completion_msg)
                self.status = TaskStatus.COMPLETED
                _log_to_file(
                    f"综合报告生成完成: html={self.report_file}, "
                    f"md={self.markdown_file}, pdf={self.pdf_file}"
                )
            
        except Exception as e:
            self.error = str(e)
            self.status = TaskStatus.ERROR
            self.update_progress(0, f"报告生成失败: {e}")
            _log_to_file(f"报告生成失败: {e}", "error")


# ==================== 分析任务类 ====================

class AnalysisTask:
    """完整的分析任务"""
    
    def __init__(
        self,
        event: AnalysisEvent,
        config: Dict[str, Any],
        stream_verbose: bool = False,
        sse_format: str = "legacy",
    ):
        self.task_id = f"analysis_{int(time.time())}_{event.event_id}"
        self.event_id = event.event_id
        self.event_type = event.event_type
        self.session_id = (event.metadata or {}).get("session_id")
        self.query = event.query or event.topic
        self.topic = event.topic
        self.config = config
        self.stream_verbose = stream_verbose
        self.sse_format = sse_format if sse_format in ("legacy", "decision") else "legacy"
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.message = ""
        self.error_reason: Optional[str] = None
        self.frozen = False
        self.pending_engines_at_failure: List[str] = []
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        
        # 引擎实例
        self.insight_executor: Optional[InsightEngineExecutor] = None
        self.media_executor: Optional[MediaEngineExecutor] = None
        self.query_executor: Optional[QueryEngineExecutor] = None
        self.forum_executor: Optional[ForumEngineExecutor] = None
        self.report_generator: Optional[ReportGenerator] = None
        self.event_bus = TaskEventBus(self.task_id)

    def emit_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None):
        """向 SSE 订阅者发送流程事件（失败不影响主任务）。"""
        try:
            self.event_bus.emit(event_type, payload or {})
        except Exception:
            pass

    def emit_workflow(self, workflow_payload: Optional[Dict[str, Any]]) -> None:
        if workflow_payload:
            self.emit_event("workflow", workflow_payload)

    def handle_report_event(self, payload: Dict[str, Any]) -> None:
        """过滤 report_stage 细粒度 delta，并映射 workflow 人话事件。"""
        if self.sse_format == "decision":
            self.emit_event("report_stage", payload)
            return
        scrubbed = scrub_report_stage_payload(payload, self.stream_verbose)
        if should_emit_report_stage(scrubbed, self.stream_verbose):
            self.emit_event("report_stage", scrubbed)
        report_event = payload.get("report_event") or ""
        self.emit_workflow(workflow_from_report_event(report_event, payload))

    def update_status(self, status: TaskStatus, progress: int, message: str):
        """更新任务状态"""
        if self.frozen and status != self.status:
            return
        self.status = status
        self.progress = progress
        self.message = message
        self.updated_at = datetime.now()
        _log_to_file(f"任务 {self.task_id} 状态更新: {status.value} ({progress}%) - {message}")
        self.emit_event(
            "task_progress",
            {"status": status.value, "progress": progress, "message": message},
        )
        self.emit_workflow(workflow_from_task_message(message, progress))

    def fail_task(
        self,
        reason: str,
        progress: Optional[int] = None,
        pending_engines: Optional[List[str]] = None,
        status: TaskStatus = TaskStatus.ERROR,
        emit_error_event: bool = True,
    ):
        """标记任务失败/取消，冻结进度回调并记录专用错误原因。"""
        self.frozen = True
        self.error_reason = reason
        if pending_engines is not None:
            self.pending_engines_at_failure = pending_engines
        self.status = status
        if progress is not None:
            self.progress = progress
        self.message = reason
        self.updated_at = datetime.now()
        _log_to_file(
            f"任务 {self.task_id} 终止: {status.value} ({self.progress}%) - {reason}",
            "error" if status == TaskStatus.ERROR else "api",
        )
        self.emit_event(
            "task_progress",
            {"status": status.value, "progress": self.progress, "message": reason},
        )
        if emit_error_event and status == TaskStatus.ERROR:
            self.emit_event(
                "task_error",
                {
                    "error_message": reason,
                    "error_reason": reason,
                    "pending_engines": self.pending_engines_at_failure,
                },
            )
    
    def get_engine_progress_callback(self):
        """获取引擎进度回调函数"""
        def callback(engine: str, progress: int, message: str):
            self.emit_event(
                "engine_progress",
                {"engine": engine, "progress": progress, "message": message},
            )
            self.emit_workflow(
                workflow_from_engine_progress(engine, progress, message)
            )
            if self.frozen:
                return

            weights = {
                'insight': 0.25,
                'media': 0.25,
                'query': 0.25,
                'forum': 0.0,
                'report': 0.25
            }
            weight = weights.get(engine, 0)
            if self.status == TaskStatus.RUNNING:
                self.progress = max(self.progress, int(weight * progress))
            self.message = f"[{engine.upper()}] {message}"
            self.updated_at = datetime.now()
            _log_to_file(f"任务 {self.task_id} [{engine}] {progress}% - {message}")
        
        return callback

    def _engine_display_status(
        self,
        engine_name: str,
        executor: EngineExecutor,
    ) -> EngineStatus:
        """超时/失败后展示引擎状态，避免滞后完成的线程误导前端。"""
        if engine_name in self.pending_engines_at_failure:
            return EngineStatus(
                engine=engine_name,
                status="cancelled",
                progress=executor.progress,
                message="任务已超时终止（引擎在后台可能仍在运行，结果未纳入本任务）",
            )
        return EngineStatus(
            engine=engine_name,
            status=executor.status.value,
            progress=executor.progress,
            message=executor.message,
        )
    
    def get_engines_status(self) -> List[EngineStatus]:
        """获取各引擎状态"""
        statuses = []
        
        if self.insight_executor:
            statuses.append(self._engine_display_status('insight', self.insight_executor))
        
        if self.media_executor:
            statuses.append(self._engine_display_status('media', self.media_executor))
        
        if self.query_executor:
            statuses.append(self._engine_display_status('query', self.query_executor))
        
        if self.forum_executor:
            statuses.append(EngineStatus(
                engine='forum',
                status=self.forum_executor.status.value,
                progress=self.forum_executor.progress,
                message=self.forum_executor.message
            ))
        
        if self.report_generator:
            statuses.append(EngineStatus(
                engine='report',
                status=self.report_generator.status.value,
                progress=self.report_generator.progress,
                message=self.report_generator.message
            ))
        
        return statuses
    
    def get_result(self) -> Optional[Dict[str, Any]]:
        """获取任务结果"""
        if self.status != TaskStatus.COMPLETED:
            return None
        
        rg = self.report_generator
        result = {
            'task_id': self.task_id,
            'query': self.query,
            'report_file': rg.report_file if rg else None,
            'ir_file_path': rg.ir_file_path if rg else None,
            'markdown_file': rg.markdown_file if rg else None,
            'markdown_ready': rg.markdown_ready if rg else False,
            'markdown_error': rg.markdown_error if rg else None,
            'markdown_url': _markdown_url_for(self.task_id) if rg and rg.markdown_ready else None,
            'pdf_file': rg.pdf_file if rg else None,
            'pdf_ready': rg.pdf_ready if rg else False,
            'pdf_error': rg.pdf_error if rg else None,
            'engines_output': {}
        }
        
        # 添加各引擎输出
        if self.insight_executor and self.insight_executor.output_file:
            result['engines_output']['insight'] = self.insight_executor.output_file
        if self.media_executor and self.media_executor.output_file:
            result['engines_output']['media'] = self.media_executor.output_file
        if self.query_executor and self.query_executor.output_file:
            result['engines_output']['query'] = self.query_executor.output_file
        
        return result
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'task_id': self.task_id,
            'event_id': self.event_id,
            'session_id': self.session_id,
            'event_type': self.event_type,
            'topic': self.topic,
            'query': self.query,
            'status': self.status.value,
            'progress': self.progress,
            'message': self.message,
            'error_reason': self.error_reason,
            'engines': [s.model_dump() for s in self.get_engines_status()],
            'report_file': self.report_generator.report_file if self.report_generator and self.report_generator.report_file else None,
            'ir_file_path': self.report_generator.ir_file_path if self.report_generator and self.report_generator.ir_file_path else None,
            'pdf_file': self.report_generator.pdf_file if self.report_generator and self.report_generator.pdf_file else None,
            'pdf_ready': self.report_generator.pdf_ready if self.report_generator else False,
            'markdown_file': self.report_generator.markdown_file if self.report_generator and self.report_generator.markdown_file else None,
            'markdown_ready': self.report_generator.markdown_ready if self.report_generator else False,
            'markdown_error': self.report_generator.markdown_error if self.report_generator else None,
            'markdown_url': _markdown_url_for(self.task_id) if self.report_generator and self.report_generator.markdown_ready else None,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat()
        }


# ==================== 全局状态 ====================

# 任务注册表
tasks_registry: Dict[str, AnalysisTask] = {}
tasks_lock = threading.Lock()

# 加载配置
def load_config() -> Dict[str, Any]:
    """从config模块加载配置"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from config import settings
        
        return {
            'DB_HOST': getattr(settings, 'DB_HOST', None),
            'DB_PORT': getattr(settings, 'DB_PORT', None),
            'DB_USER': getattr(settings, 'DB_USER', None),
            'DB_PASSWORD': getattr(settings, 'DB_PASSWORD', None),
            'DB_NAME': getattr(settings, 'DB_NAME', None),
            'DB_CHARSET': getattr(settings, 'DB_CHARSET', None),
            'DB_DIALECT': getattr(settings, 'DB_DIALECT', None),
            'INSIGHT_ENGINE_API_KEY': getattr(settings, 'INSIGHT_ENGINE_API_KEY', None),
            'INSIGHT_ENGINE_BASE_URL': getattr(settings, 'INSIGHT_ENGINE_BASE_URL', None),
            'INSIGHT_ENGINE_MODEL_NAME': getattr(settings, 'INSIGHT_ENGINE_MODEL_NAME', None),
            'MEDIA_ENGINE_API_KEY': getattr(settings, 'MEDIA_ENGINE_API_KEY', None),
            'MEDIA_ENGINE_BASE_URL': getattr(settings, 'MEDIA_ENGINE_BASE_URL', None),
            'MEDIA_ENGINE_MODEL_NAME': getattr(settings, 'MEDIA_ENGINE_MODEL_NAME', None),
            'QUERY_ENGINE_API_KEY': getattr(settings, 'QUERY_ENGINE_API_KEY', None),
            'QUERY_ENGINE_BASE_URL': getattr(settings, 'QUERY_ENGINE_BASE_URL', None),
            'QUERY_ENGINE_MODEL_NAME': getattr(settings, 'QUERY_ENGINE_MODEL_NAME', None),
            'TAVILY_API_KEY': getattr(settings, 'TAVILY_API_KEY', None),
            'BOCHA_WEB_SEARCH_API_KEY': getattr(settings, 'BOCHA_WEB_SEARCH_API_KEY', None),
            'ANSPIRE_API_KEY': getattr(settings, 'ANSPIRE_API_KEY', None),
            'SEARCH_TOOL_TYPE': getattr(settings, 'SEARCH_TOOL_TYPE', 'BochaAPI'),
            'REPORT_ENGINE_API_KEY': getattr(settings, 'REPORT_ENGINE_API_KEY', None),
            'REPORT_ENGINE_BASE_URL': getattr(settings, 'REPORT_ENGINE_BASE_URL', None),
            'REPORT_ENGINE_MODEL_NAME': getattr(settings, 'REPORT_ENGINE_MODEL_NAME', None),
            'KEYWORD_OPTIMIZER_API_KEY': getattr(settings, 'KEYWORD_OPTIMIZER_API_KEY', None),
            'KEYWORD_OPTIMIZER_BASE_URL': getattr(settings, 'KEYWORD_OPTIMIZER_BASE_URL', None),
            'KEYWORD_OPTIMIZER_MODEL_NAME': getattr(settings, 'KEYWORD_OPTIMIZER_MODEL_NAME', None),
        }
    except Exception as e:
        _log_to_file(f"加载配置失败: {e}", "error")
        return {}


# ==================== 任务执行函数 ====================

def run_full_analysis_task(task: AnalysisTask):
    """执行完整的分析任务流程"""
    with bind_log_stream_task(task):
        _run_full_analysis_task_impl(task)


def _run_full_analysis_task_impl(task: AnalysisTask):
    try:
        task.emit_event("task_started", {
            "query": task.query,
            "topic": task.topic,
            "session_id": task.session_id,
        })
        task.emit_workflow(
            build_workflow_payload(
                "system",
                "start",
                f"已接收分析主题「{task.topic or task.query}」，正在启动任务…",
                progress_hint=0,
            )
        )
        task.update_status(TaskStatus.RUNNING, 0, "任务已启动，正在初始化...")
        
        # 检查mock_search_api
        if not check_mock_search_api():
            task.fail_task("mock_search_api服务不可用，请确保已启动", progress=0)
            return
        
        # 1. 启动ForumEngine监控
        task.update_status(TaskStatus.RUNNING, 5, "正在启动ForumEngine监控...")
        task.forum_executor = ForumEngineExecutor(progress_callback=task.get_engine_progress_callback())
        task.forum_executor.start()
        time.sleep(2)  # 等待ForumEngine启动
        
        # 2. 并发启动三个分析引擎
        task.update_status(TaskStatus.RUNNING, 10, "正在启动三个分析引擎...")
        
        # 启动InsightEngine
        task.insight_executor = InsightEngineExecutor(
            query=task.query,
            config=task.config,
            progress_callback=task.get_engine_progress_callback()
        )
        task.insight_executor.start()
        
        # 启动MediaEngine
        task.media_executor = MediaEngineExecutor(
            query=task.query,
            config=task.config,
            progress_callback=task.get_engine_progress_callback()
        )
        task.media_executor.start()
        
        # 启动QueryEngine
        task.query_executor = QueryEngineExecutor(
            query=task.query,
            config=task.config,
            progress_callback=task.get_engine_progress_callback()
        )
        task.query_executor.start()
        
        task.update_status(TaskStatus.RUNNING, 15, "三个分析引擎正在运行...")
        
        # 3. 等待三个引擎完成
        max_wait_time = ANALYSIS_ENGINE_MAX_WAIT_SECONDS
        start_time = time.time()
        
        while True:
            if time.time() - start_time > max_wait_time:
                pending_engines = []
                if task.insight_executor.status not in [TaskStatus.COMPLETED, TaskStatus.ERROR]:
                    pending_engines.append("insight")
                if task.media_executor.status not in [TaskStatus.COMPLETED, TaskStatus.ERROR]:
                    pending_engines.append("media")
                if task.query_executor.status not in [TaskStatus.COMPLETED, TaskStatus.ERROR]:
                    pending_engines.append("query")
                completed_count = sum([
                    task.insight_executor.status in [TaskStatus.COMPLETED, TaskStatus.ERROR],
                    task.media_executor.status in [TaskStatus.COMPLETED, TaskStatus.ERROR],
                    task.query_executor.status in [TaskStatus.COMPLETED, TaskStatus.ERROR],
                ])
                timeout_progress = 15 + int((completed_count / 3) * 55)
                task.fail_task(
                    f"任务执行超时（超过 {max_wait_time} 秒）",
                    progress=timeout_progress,
                    pending_engines=pending_engines,
                )
                return
            
            # 检查三个引擎的状态
            insight_done = task.insight_executor.status in [TaskStatus.COMPLETED, TaskStatus.ERROR]
            media_done = task.media_executor.status in [TaskStatus.COMPLETED, TaskStatus.ERROR]
            query_done = task.query_executor.status in [TaskStatus.COMPLETED, TaskStatus.ERROR]
            
            # 计算进度 (15% - 70%)
            completed_count = sum([insight_done, media_done, query_done])
            engine_progress = (completed_count / 3) * 55  # 55%用于引擎执行
            task.update_status(
                TaskStatus.RUNNING,
                15 + int(engine_progress),
                f"分析引擎执行中... ({completed_count}/3 完成)"
            )
            
            if completed_count == 3:
                break
            
            # 检查是否有引擎失败
            failed_engines = []
            if task.insight_executor.status == TaskStatus.ERROR:
                failed_engines.append('insight')
            if task.media_executor.status == TaskStatus.ERROR:
                failed_engines.append('media')
            if task.query_executor.status == TaskStatus.ERROR:
                failed_engines.append('query')
            
            if failed_engines:
                _log_to_file(f"部分引擎执行失败: {failed_engines}", "warning")
            
            time.sleep(5)
        
        # 检查引擎执行结果
        if all(e.status == TaskStatus.COMPLETED for e in [task.insight_executor, task.media_executor, task.query_executor]):
            task.update_status(TaskStatus.RUNNING, 70, "三个分析引擎已完成，正在生成综合报告...")
        else:
            task.update_status(TaskStatus.RUNNING, 70, "部分分析引擎完成，正在生成综合报告...")
        
        # 4. 准备引擎输出
        engines_output = {
            'insight': task.insight_executor.output_file if task.insight_executor.output_file else "",
            'media': task.media_executor.output_file if task.media_executor.output_file else "",
            'query': task.query_executor.output_file if task.query_executor.output_file else "",
        }
        
        # 5. 生成综合报告
        task.update_status(TaskStatus.RUNNING, 75, "正在调用ReportEngine生成综合报告...")
        task.report_generator = ReportGenerator(
            engines_output=engines_output,
            query=task.query,
            progress_callback=task.get_engine_progress_callback(),
            report_event_emitter=task.handle_report_event,
            stream_markdown=(task.sse_format == "decision"),
        )
        task.report_generator.run()
        
        # 6. 完成任务
        if task.report_generator.status == TaskStatus.COMPLETED:
            task.update_status(TaskStatus.COMPLETED, 100, "分析任务全部完成")
            _log_to_file(f"任务 {task.task_id} 全部完成，报告文件: {task.report_generator.report_file}")
            rg = task.report_generator
            task.emit_event(
                "task_completed",
                {
                    "progress": 100,
                    "session_id": task.session_id,
                    "report_file": rg.report_file,
                    "ir_file_path": rg.ir_file_path,
                    "markdown_file": rg.markdown_file,
                    "markdown_ready": rg.markdown_ready,
                    "markdown_error": rg.markdown_error,
                    "markdown_url": _markdown_url_for(task.task_id) if rg.markdown_ready else None,
                    "pdf_file": rg.pdf_file,
                    "pdf_ready": rg.pdf_ready,
                    "pdf_error": rg.pdf_error,
                    "message": "分析任务全部完成",
                },
            )
            task.emit_workflow(
                build_workflow_payload(
                    "system",
                    "done",
                    "分析任务全部完成，报告已就绪",
                    level="success",
                    progress_hint=100,
                )
            )
        else:
            error_message = f"报告生成失败: {task.report_generator.error}"
            task.fail_task(
                error_message,
                progress=task.report_generator.progress,
                emit_error_event=False,
            )
            task.emit_event(
                "task_error",
                {"error_message": error_message, "error_reason": error_message, "engine": "report"},
            )
        
        # 7. 停止ForumEngine监控
        if task.forum_executor:
            task.forum_executor.stop()
        
    except Exception as e:
        _log_to_file(f"任务 {task.task_id} 执行异常: {e}", "error")
        task.fail_task(f"任务执行异常: {str(e)}", progress=0)
        
        # 尝试停止ForumEngine
        if task.forum_executor:
            task.forum_executor.stop()


# ==================== FastAPI应用 ====================


# lifespan event handler (replaces deprecated on_event)
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management"""
    # Startup
    logger.info("=" * 60)
    logger.info("BettaFish Analysis System Integration API starting...")
    logger.info("=" * 60)
    logger.info(f"Listening: http://{API_HOST}:{API_PORT}")
    logger.info(f"Docs: http://{API_HOST}:{API_PORT}/docs")

    # Check dependencies
    if check_mock_search_api():
        logger.info("OK: mock_search_api is ready")
    else:
        logger.warning("WARN: mock_search_api is not available, please start it first")

    # Load config
    config = load_config()
    logger.info("OK: Config loaded")
    _enable_fb = os.getenv("ENABLE_DB_FALLBACK", "false").lower() in ("true", "1", "yes")
    logger.info(
        f"配置: ENABLE_DB_FALLBACK={_enable_fb}, "
        f"ANALYSIS_ENGINE_MAX_WAIT_SECONDS={ANALYSIS_ENGINE_MAX_WAIT_SECONDS}"
    )

    # Check engines config
    if config.get('INSIGHT_ENGINE_API_KEY'):
        logger.info(f"OK: InsightEngine configured (model: {config.get('INSIGHT_ENGINE_MODEL_NAME')})")
    else:
        logger.warning("WARN: InsightEngine API key not configured")

    if config.get('MEDIA_ENGINE_API_KEY'):
        logger.info(f"OK: MediaEngine configured (model: {config.get('MEDIA_ENGINE_MODEL_NAME')})")
    else:
        logger.warning("WARN: MediaEngine API key not configured")

    if config.get('QUERY_ENGINE_API_KEY'):
        logger.info(f"OK: QueryEngine configured (model: {config.get('QUERY_ENGINE_MODEL_NAME')})")
    else:
        logger.warning("WARN: QueryEngine API key not configured")

    if config.get('REPORT_ENGINE_API_KEY'):
        logger.info(f"OK: ReportEngine configured (model: {config.get('REPORT_ENGINE_MODEL_NAME')})")
    else:
        logger.warning("WARN: ReportEngine API key not configured")

    logger.info("=" * 60)
    logger.info("API service is ready")
    logger.info("=" * 60)

    setup_analysis_log_stream_sink()

    def _refresh_module_health_cache() -> None:
        """后台刷新引擎 import 状态；失败只写缓存，绝不影响进程存活。"""
        from datetime import datetime as _dt

        def _probe(import_fn):
            try:
                import_fn()
                return "available"
            except Exception as e:
                return f"unavailable: {e}"

        try:
            status = {
                "InsightEngine": _probe(
                    lambda: __import__(
                        "InsightEngine", fromlist=["DeepSearchAgent"]
                    ).DeepSearchAgent
                ),
                "MediaEngine": _probe(
                    lambda: __import__(
                        "MediaEngine", fromlist=["DeepSearchAgent"]
                    ).DeepSearchAgent
                ),
                "QueryEngine": _probe(
                    lambda: __import__(
                        "QueryEngine", fromlist=["DeepSearchAgent"]
                    ).DeepSearchAgent
                ),
                "ForumEngine": _probe(
                    lambda: __import__(
                        "ForumEngine.monitor", fromlist=["LogMonitor"]
                    ).LogMonitor
                ),
                "ReportEngine": _probe(
                    lambda: __import__(
                        "ReportEngine.agent", fromlist=["ReportAgent"]
                    ).ReportAgent
                ),
            }
            _module_health_cache["status"] = status
            _module_health_cache["last_check"] = _dt.now()
        except Exception as exc:
            logger.warning("后台引擎健康探测失败（忽略）: {}", exc)

    # 启动后异步刷新一次；不堵 lifespan，不堵 Docker 探活
    threading.Thread(
        target=_refresh_module_health_cache,
        name="module-health-probe",
        daemon=True,
    ).start()

    yield  # Application runs here

    # Shutdown
    logger.info("Shutting down API service...")

    # Stop all running tasks
    with tasks_lock:
        for task_id, task in tasks_registry.items():
            if task.status == TaskStatus.RUNNING:
                if task.forum_executor:
                    task.forum_executor.stop()
                task.fail_task("Service shutdown", status=TaskStatus.CANCELLED, emit_error_event=False)

    logger.info("API service shutdown complete")

app = FastAPI(
    title=API_TITLE,
    description="舆情分析系统集成接口，支持完整的智能体协作流程",
    version=API_VERSION,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# ---- 离线 Swagger / ReDoc（静态文件：static/swagger-ui/，不依赖外网 CDN）----
_SWAGGER_STATIC_DIR = PROJECT_ROOT / "static" / "swagger-ui"
if _SWAGGER_STATIC_DIR.is_dir():
    app.mount(
        "/static/swagger-ui",
        StaticFiles(directory=str(_SWAGGER_STATIC_DIR)),
        name="swagger-ui-static",
    )


@app.get("/docs", include_in_schema=False)
async def swagger_ui_offline():
    """内场离线可用的 Swagger UI。"""
    js_name = "swagger-ui-bundle.js"
    css_name = "swagger-ui.css"
    if not (_SWAGGER_STATIC_DIR / js_name).is_file() or not (
        _SWAGGER_STATIC_DIR / css_name
    ).is_file():
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "离线文档静态资源缺失。请将 swagger-ui-bundle.js / swagger-ui.css "
                    f"放到 {_SWAGGER_STATIC_DIR}"
                )
            },
        )
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{API_TITLE} - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger-ui/favicon-32x32.png",
    )


@app.get("/redoc", include_in_schema=False)
async def redoc_offline():
    """内场离线可用的 ReDoc。"""
    redoc_js = "redoc.standalone.js"
    if not (_SWAGGER_STATIC_DIR / redoc_js).is_file():
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    f"离线 ReDoc 资源缺失。可将 {redoc_js} 放到 {_SWAGGER_STATIC_DIR}；"
                    "Swagger UI 请访问 /docs"
                )
            },
        )
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"{API_TITLE} - ReDoc",
        redoc_js_url="/static/swagger-ui/redoc.standalone.js",
        redoc_favicon_url="/static/swagger-ui/favicon-32x32.png",
    )

# 本地前端跨域：显式白名单 + localhost 任意端口正则。
# Starlette 对不在名单内的 Origin，预检 OPTIONS 会直接 400 → 浏览器报 Failed to fetch。
_cors_origins_raw = os.getenv(
    "CORS_ALLOW_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,"
    "http://127.0.0.1:5173,http://localhost:5173,"
    "http://127.0.0.1:8080,http://localhost:8080,"
    "null",
)
CORS_ALLOW_ORIGINS = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
# 覆盖 Live Server / http.server 等任意本机端口；也可在 .env 覆盖
CORS_ALLOW_ORIGIN_REGEX = os.getenv(
    "CORS_ALLOW_ORIGIN_REGEX",
    # 本机任意端口 + 局域网 192.168.x.x（内网联调）；生产请收紧
    r"https?://(localhost|127\.0\.0\.1|\[::1\]|192\.168\.\d{1,3}\.\d{1,3})(:\d+)?$",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# 事件页结构化接口（impact / open events / sensitive-analysis）——与 multi-agent / event2 解耦
from event_report import router as event_report_router

app.include_router(event_report_router)

# 厂家舆情采集：按天同步左屏事件 + 进度查询（对接「对外厂家舆情采集」文档）
from opinion_crawl import router as opinion_crawl_router

app.include_router(opinion_crawl_router)
from crawl_import import router as crawl_import_router

app.include_router(crawl_import_router)

# 全局报告模板增删改（与 session 无关；落盘 logs/custom_report_templates/）
from singleAgent.session_templates_api import router as session_templates_router

app.include_router(session_templates_router)


@app.get("/", tags=["Root"])
async def root():
    """API根路径"""
    return {
        "name": API_TITLE,
        "version": API_VERSION,
        "message": "舆情分析系统集成接口",
        "docs": "/docs",
        "acceptance_demo": "/acceptance_demo.html",
        "event_coverage": "/event_coverage.html",
    }


@app.get("/acceptance_demo.html", include_in_schema=False)
async def acceptance_demo_page():
    """验收演示页：与 API 同域托管，避免跨域 OPTIONS 预检 400。"""
    path = PROJECT_ROOT / "acceptance_demo.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="acceptance_demo.html 未找到")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/event_coverage.html", include_in_schema=False)
async def event_coverage_page():
    path = PROJECT_ROOT / "event_coverage.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="event_coverage.html 未找到")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/event_eval_sample.json", include_in_schema=False)
async def event_eval_sample():
    path = PROJECT_ROOT / "event_eval_sample.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="event_eval_sample.json 未找到")
    return FileResponse(path, media_type="application/json")


@app.get("/health", response_model=HealthCheckResponse, tags=["Health"])
async def health_check():
    """进程探活（Docker healthcheck）。

    **绝不**因 mock / 卫鼎 / DM / 引擎 import 失败返回非 200，也绝不在探活路径上
    同步打外部 HTTP 或冷启动重模块——否则 compose 5s 超时 → unhealthy → autoheal
    重启，等于「拿别人的错误惩罚自己」。依赖详情仅读缓存；刷新放 lifespan 后台。
    """
    t0 = time.time()
    from datetime import datetime as _dt

    if _health_cache["last_check"] is not None:
        mock_api_status = (
            "available" if _health_cache["available"] else "unavailable"
        )
    else:
        mock_api_status = "unknown"

    dependencies: Dict[str, str] = {
        "mock_search_api": mock_api_status,
    }

    modules_status = _module_health_cache.get("status")
    if isinstance(modules_status, dict):
        dependencies.update(modules_status)
    else:
        dependencies.update(
            {
                "InsightEngine": "not_probed",
                "MediaEngine": "not_probed",
                "QueryEngine": "not_probed",
                "ForumEngine": "not_probed",
                "ReportEngine": "not_probed",
            }
        )

    # 进程活着即 healthy；依赖仅作 informational（degraded 不改 HTTP 码）
    known = [v for v in dependencies.values() if v not in ("unknown", "not_probed")]
    all_ok = bool(known) and all(v == "available" for v in known)
    overall_status = "healthy" if (not known or all_ok) else "degraded"

    elapsed_ms = (time.time() - t0) * 1000
    if elapsed_ms >= 500:
        from singleAgent.diag_codes import E2201_HEALTH_SLOW, format_diag

        logger.warning(
            format_diag(
                E2201_HEALTH_SLOW,
                "health_slow",
                elapsed_ms=f"{elapsed_ms:.1f}",
                status=overall_status,
                mock=mock_api_status,
            )
        )

    return HealthCheckResponse(
        status=overall_status,
        mock_search_api=mock_api_status,
        dependencies=dependencies,
        timestamp=_dt.now().isoformat(),
    )


@app.post("/api/v1/analysis/event", tags=["Analysis"])
async def receive_analysis_event(body: AnalysisEventRequest, request: Request):
    """
    接收分析事件（辅助决策前端）

    主要字段：instruction、session_id、knowledge_base_ids、ext（案例）、
    files_data（附件 name/type/content）、model_config（id/base_url/api_key）。

    Accept: application/json → task_id；Accept: text/event-stream → decision SSE。
    仍兼容 v1：event_id + topic + query。
    """
    event_id_for_error = body.session_id or body.event_id or "unknown"
    try:
        analysis_event = body.to_analysis_event()
        _log_to_file(
            f"收到分析事件: session={body.session_id}, event_id={analysis_event.event_id}, "
            f"topic={analysis_event.topic[:60]}, "
            f"files={len(body.files_data)}, kb={body.knowledge_base_ids}"
        )

        if not check_mock_search_api():
            _log_to_file(
                f"mock_search_api不可用，无法处理事件 {analysis_event.event_id}", "error"
            )
            return AnalysisResponse(
                success=False,
                event_id=analysis_event.event_id,
                message="mock_search_api服务不可用，请确保已启动",
                data={
                    'hint': '请先启动 mock_search_api/main.py 服务'
                }
            )

        config = load_config()
        if not config:
            return AnalysisResponse(
                success=False,
                event_id=analysis_event.event_id,
                message="配置加载失败"
            )

        llm = body.resolve_llm()
        if any(llm.values()):
            config = _apply_llm_overrides(config, llm)
            _log_to_file(
                f"已应用前端 model_config: model={llm.get('model_name')}, "
                f"base_url={llm.get('base_url')}"
            )

        stream_verbose = _parse_stream_verbose(request, analysis_event.metadata)
        sse_format = _parse_sse_format(request, analysis_event.metadata)
        accept = (request.headers.get("accept") or "application/json").lower()
        if (
            sse_format == "legacy"
            and (body.instruction or body.ext or body.files_data)
            and "text/event-stream" in accept
        ):
            sse_format = "decision"
        task = AnalysisTask(
            analysis_event,
            config,
            stream_verbose=stream_verbose,
            sse_format=sse_format,
        )

        with tasks_lock:
            tasks_registry[task.task_id] = task

        thread = threading.Thread(
            target=run_full_analysis_task,
            args=(task,),
            daemon=True
        )
        thread.start()

        _log_to_file(f"任务已创建: {task.task_id} (session={task.session_id})")

        accept = (request.headers.get("accept") or "application/json").lower()
        if "text/event-stream" in accept:
            return StreamingResponse(
                _sse_generator_for_task(task, emit_task_created=True),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return AnalysisResponse(
            success=True,
            event_id=analysis_event.event_id,
            task_id=task.task_id,
            message="事件已接收，分析任务已启动（包含三个引擎并发执行）",
            data={
                'task_id': task.task_id,
                'session_id': task.session_id,
                'status': task.status.value,
                'progress': task.progress,
                'topic': task.topic,
                'query': task.query,
                'stream_url': f'/api/v1/analysis/stream/{task.task_id}',
                'markdown_url': _markdown_url_for(task.task_id),
            }
        )

    except Exception as e:
        _log_to_file(f"处理分析事件失败: {e}", "error")
        return AnalysisResponse(
            success=False,
            event_id=event_id_for_error,
            message=f"处理失败: {str(e)}"
        )


@app.post("/api/v1/analysis/event2", tags=["Analysis"])
async def receive_analysis_event2(body: AnalysisEventRequest, request: Request):
    """
    singleAgent 快模式：多轮达梦检索 + LLM 真流式 Markdown（decision SSE）。

    请求体与 /event 相同。请使用 Accept: text/event-stream。
    """
    from singleAgent import run_fast_analysis

    event_id_for_error = body.session_id or body.event_id or "unknown"
    session_id = event_id_for_error
    session_store = None
    cancellation_handle = None
    cancellation_registry = None
    try:
        analysis_event = body.to_analysis_event()
        llm = body.resolve_llm()
        session_id = (analysis_event.metadata or {}).get("session_id") or analysis_event.event_id
        from singleAgent.session_store import get_session_store
        from singleAgent.cancellation import (
            CrossSessionPreemptionError,
            get_cancellation_registry,
        )

        session_store = get_session_store(PROJECT_ROOT)
        cancellation_registry = get_cancellation_registry()
        request_id = (analysis_event.metadata or {}).get("request_id") or ""
        source_request_id = (
            (analysis_event.metadata or {}).get("source_request_id") or ""
        )
        try:
            registration = cancellation_registry.register_or_preempt(
                request_id,
                session_id,
                source_request_id=source_request_id,
            )
        except CrossSessionPreemptionError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        cancellation_handle = registration.handle

        if registration.preempted_handles:
            # 只发取消信号，绝不 await to_thread(wait_terminal)：
            # 线程池一旦被卫鼎/LLM 同步调用占满，事件循环会在这里饿死，/health 也会挂。
            _log_to_file(
                f"[event2] session={session_id}, request={request_id}, "
                f"preempted={','.join(registration.preempted_request_ids)} "
                f"(no wait_terminal)"
            )

        from singleAgent.template_selection import (
            TEMPLATE_GENERATION_STAGE,
            TEMPLATE_SELECTION_STAGE,
            merge_generation_instruction,
            merge_knowledge_base_ids,
            resolve_interaction_stage,
            stream_template_selection,
        )

        meta = dict(analysis_event.metadata or {})
        interaction_stage = resolve_interaction_stage(meta)
        accept = (request.headers.get("accept") or "application/json").lower()

        if interaction_stage == TEMPLATE_SELECTION_STAGE:
            if "text/event-stream" not in accept:
                raise HTTPException(
                    status_code=406,
                    detail="template_selection 请使用 Accept: text/event-stream",
                )
            from singleAgent.report_template_loader import load_all_report_templates

            try:
                from singleAgent.event2_stream import run_in_event2_executor

                templates = await run_in_event2_executor(
                    load_all_report_templates,
                    project_root=PROJECT_ROOT,
                    session_id=session_id,
                    timeout=float(os.getenv("AGENT_TEMPLATE_LOAD_TIMEOUT_SEC", "8") or "8"),
                )
            except asyncio.TimeoutError as error:
                raise HTTPException(
                    status_code=504, detail="报告模板加载超时（已避免拖死事件循环）"
                ) from error
            except Exception as error:
                raise HTTPException(status_code=500, detail=f"报告模板加载失败: {error}") from error
            if not templates:
                raise HTTPException(status_code=500, detail="未配置可用报告模板")

            instruction = (meta.get("instruction") or "").strip()
            session_store.update_session_info(
                session_id,
                user_id=body.user_id,
                agent_id=body.agent_id or 5,
                agent_name=body.agent_name or "舆情事件",
                title=body.title or _short_title(instruction),
            )
            pending = session_store.save_pending_template(
                session_id,
                request_id=request_id,
                instruction=instruction,
                knowledge_base_ids=_normalize_knowledge_base_ids(body.knowledge_base_ids),
            )
            _log_to_file(
                f"[event2] session={session_id}, request={request_id}, "
                "interaction_stage=template_selection awaiting_template=true external_calls=0"
            )

            def template_selection_generator():
                from singleAgent.sse import attach_event_identity

                status = "failed"
                try:
                    for frame in stream_template_selection(
                        templates,
                        session_id=session_id,
                        pending_request_id=pending.request_id,
                        expires_at=pending.expires_at,
                    ):
                        yield attach_event_identity(
                            frame,
                            request_id=request_id,
                            session_id=session_id,
                        )
                    status = "completed"
                except GeneratorExit:
                    status = "cancelled"
                    raise
                finally:
                    cancellation_registry.finish(request_id, status)

            from singleAgent.event2_stream import (
                Event2StreamBusy,
                iter_sync_sse,
                try_acquire_event2_stream_slot,
            )

            try:
                try_acquire_event2_stream_slot()
            except Event2StreamBusy as busy:
                cancellation_registry.finish(request_id, "failed")
                raise HTTPException(status_code=503, detail=str(busy)) from busy

            async def template_selection_agen():
                async for frame in iter_sync_sse(
                    template_selection_generator,
                    label="template_selection",
                    slot_acquired=True,
                ):
                    yield frame

            return StreamingResponse(
                template_selection_agen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-Request-ID": request_id,
                },
            )

        if interaction_stage == TEMPLATE_GENERATION_STAGE:
            from singleAgent.report_template_loader import load_report_template, resolve_template_id
            from singleAgent.session_store import PendingTemplateError

            try:
                load_report_template(resolve_template_id(meta), project_root=PROJECT_ROOT)
            except Exception as error:
                raise HTTPException(status_code=422, detail=f"报告模板无效: {error}") from error
            try:
                pending = session_store.consume_pending_template(
                    session_id,
                    request_id=source_request_id,
                )
            except PendingTemplateError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

            merged_instruction = merge_generation_instruction(
                pending.instruction,
                meta.get("instruction") or "",
            )
            merged_kb_ids = merge_knowledge_base_ids(
                pending.knowledge_base_ids,
                meta.get("knowledge_base_ids") or [],
            )
            meta["instruction"] = merged_instruction
            meta["knowledge_base_ids"] = merged_kb_ids
            meta["pending_template_request_id"] = pending.request_id
            analysis_event.metadata = meta
            body.knowledge_base_ids = merged_kb_ids
            analysis_event.topic = (body.topic or "").strip() or _derive_topic_from_request(
                merged_instruction,
                body.ext,
                body.files_data,
            )
            analysis_event.query = (body.query or "").strip() or _build_analysis_query(
                merged_instruction,
                body.ext,
                body.files_data,
            )
            _log_to_file(
                f"[event2] session={session_id}, request={request_id}, "
                f"interaction_stage=template_generation pending={pending.request_id}"
            )

        # 附件 content 与 instruction 等价：入口合并，供历史与下游统一使用
        from singleAgent.ext_utils import apply_attachment_merge_to_metadata

        meta = dict(analysis_event.metadata or {})
        merged_user_prompt = apply_attachment_merge_to_metadata(meta)
        analysis_event.metadata = meta
        if merged_user_prompt:
            # 主题/query 优先基于合并后的用户输入，避免只看见短 instruction
            if not (body.topic or "").strip():
                analysis_event.topic = _derive_topic_from_instruction(
                    merged_user_prompt
                )
            if not (body.query or "").strip():
                analysis_event.query = merged_user_prompt

        # 空 instruction + 已选模板：用会话主题补默认意图（非首轮才有模板可选）
        from singleAgent.ext_utils import (
            default_instruction_for_template_report,
            ext_has_template_selection,
        )

        meta = dict(analysis_event.metadata or {})
        raw_instruction = (meta.get("instruction") or "").strip()
        ext_payload = meta.get("ext") if isinstance(meta.get("ext"), dict) else {}
        if not raw_instruction and ext_has_template_selection(ext_payload):
            sess = session_store.get(session_id)
            session_topic = (getattr(sess, "last_topic", None) or "").strip()
            filled = default_instruction_for_template_report(session_topic)
            meta["instruction"] = filled
            meta["attachments_merged_into_instruction"] = True
            analysis_event.metadata = meta
            if session_topic and (
                not (analysis_event.topic or "").strip()
                or analysis_event.topic.strip() in ("未命名主题", "分析报告")
            ):
                analysis_event.topic = session_topic
                analysis_event.query = session_topic
            user_content = filled
        else:
            user_content = (
                raw_instruction
                or body.instruction
                or analysis_event.query
                or analysis_event.topic
                or ""
            )

        human_metadata = _build_event2_human_metadata(body, analysis_event, llm)
        from singleAgent.event2_stream import run_in_event2_executor

        try:
            await run_in_event2_executor(
                session_store.record_human_turn,
                session_id,
                content=user_content,
                user_id=body.user_id,
                agent_id=body.agent_id or 5,
                agent_name=body.agent_name or "舆情事件",
                title=body.title
                or _short_title(user_content or analysis_event.topic),
                metadata=human_metadata,
                timeout=float(os.getenv("EVENT2_RECORD_TURN_TIMEOUT_SEC", "5") or "5"),
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"record_human_turn 超时 session={session_id} request={request_id}"
            )
        except Exception as record_error:
            logger.warning(
                f"record_human_turn 失败 session={session_id}: {record_error}"
            )
        _log_to_file(
            f"[event2] session={session_id}, topic={analysis_event.topic[:60]}, "
            f"files={len(body.files_data)}"
        )

        from singleAgent.event2_retrieval import (
            check_event2_prerequisites,
            is_event2_agent_retrieval,
            is_event2_mcp_retrieval,
        )

        # 前置检查（可能同步连 DM）走专用池，绝不抢 anyio 默认 to_thread
        from singleAgent.event2_stream import run_in_event2_executor

        _pre_timeout = float(os.getenv("EVENT2_PREFLIGHT_TIMEOUT_SEC", "5") or "5")
        try:
            pre_ok, pre_src, pre_msg = await run_in_event2_executor(
                check_event2_prerequisites,
                timeout=_pre_timeout,
            )
        except asyncio.TimeoutError:
            pre_ok, pre_src, pre_msg = (
                False,
                "timeout",
                f"前置检查超过 {_pre_timeout}s",
            )
        if not pre_ok:
            cancellation_registry.finish(request_id, "failed")
            err_text = (
                f"达梦库不可用，无法启动 event2（{pre_msg}）"
                if pre_src in {"agent", "context", "mcp"}
                else f"event2 前置检查失败（{pre_msg}）"
            )
            session_store.record_ai_turn(
                session_id,
                content=err_text,
                metadata={
                    "module_name": "舆情事件分析",
                    "mode": None,
                    "mode_name": "前置校验",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                    "knowledge": {"knowledge_base_ids": human_metadata["knowledge_base_ids"], "references": []},
                    "sse": {"thinking_done": False, "result_done": False, "reference_done": True},
                    "errors": [err_text],
                },
            )
            return AnalysisResponse(
                success=False,
                event_id=analysis_event.event_id,
                message=err_text,
                data={"hint": "请检查 /app/.env 中 DB_* 与 DM_MCP_SCHEMA"},
            )

        if not is_event2_mcp_retrieval():
            try:
                mock_ok = await run_in_event2_executor(
                    check_mock_search_api,
                    timeout=float(os.getenv("HEALTH_MOCK_TIMEOUT_SEC", "2") or "2") + 1.0,
                )
            except asyncio.TimeoutError:
                mock_ok = False
            if not mock_ok:
                cancellation_registry.finish(request_id, "failed")
                session_store.record_ai_turn(
                    session_id,
                    content="mock_search_api服务不可用，请确保已启动",
                    metadata={
                        "module_name": "舆情事件分析",
                        "mode": None,
                        "mode_name": "前置校验",
                        "markdown_ready": False,
                        "reports": [],
                        "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                        "knowledge": {
                            "knowledge_base_ids": human_metadata["knowledge_base_ids"],
                            "references": [],
                        },
                        "sse": {
                            "thinking_done": False,
                            "result_done": False,
                            "reference_done": True,
                        },
                        "errors": ["mock_search_api服务不可用"],
                    },
                )
                return AnalysisResponse(
                    success=False,
                    event_id=analysis_event.event_id,
                    message="mock_search_api服务不可用，请确保已启动",
                    data={"hint": "请先启动 mock_search_api 服务"},
                )

        config = load_config()
        if not config:
            cancellation_registry.finish(request_id, "failed")
            session_store.record_ai_turn(
                session_id,
                content="配置加载失败",
                metadata={
                    "module_name": "舆情事件分析",
                    "mode": None,
                    "mode_name": "前置校验",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                    "knowledge": {"knowledge_base_ids": human_metadata["knowledge_base_ids"], "references": []},
                    "sse": {"thinking_done": False, "result_done": False, "reference_done": True},
                    "errors": ["配置加载失败"],
                },
            )
            return AnalysisResponse(
                success=False,
                event_id=analysis_event.event_id,
                message="配置加载失败",
            )

        if any(llm.values()):
            config = _apply_llm_overrides(config, llm)

        if "text/event-stream" not in accept:
            cancellation_registry.finish(request_id, "failed")
            session_store.record_ai_turn(
                session_id,
                content="event2 请使用 Accept: text/event-stream 获取流式输出",
                metadata={
                    "module_name": "舆情事件分析",
                    "mode": None,
                    "mode_name": "前置校验",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                    "knowledge": {"knowledge_base_ids": human_metadata["knowledge_base_ids"], "references": []},
                    "sse": {"thinking_done": False, "result_done": False, "reference_done": True},
                    "errors": ["缺少 Accept: text/event-stream"],
                },
            )
            return AnalysisResponse(
                success=False,
                event_id=analysis_event.event_id,
                message="event2 请使用 Accept: text/event-stream 获取流式输出",
                data={"hint": "与 /event 的 SSE 模式相同，路径改为 /api/v1/analysis/event2"},
            )

        def event2_generator():
            from singleAgent.sse import attach_event_identity

            status = "failed"
            try:
                for frame in run_fast_analysis(
                    analysis_event,
                    config,
                    llm_override=llm,
                    project_root=PROJECT_ROOT,
                    cancellation=cancellation_handle,
                ):
                    yield attach_event_identity(
                        frame,
                        request_id=request_id,
                        session_id=session_id,
                    )
                status = "cancelled" if cancellation_handle.cancelled else "completed"
            except GeneratorExit:
                # 生成器线程内禁止 join closer，否则易与 stream.close 互锁，占死线程池
                cancellation_handle.request_cancel("disconnect", join_closers=False)
                status = "cancelled"
                raise
            finally:
                cancellation_registry.finish(request_id, status)

        from singleAgent.event2_stream import (
            Event2StreamBusy,
            iter_sync_sse,
            try_acquire_event2_stream_slot,
        )

        try:
            try_acquire_event2_stream_slot()
        except Event2StreamBusy as busy:
            cancellation_registry.finish(request_id, "failed")
            raise HTTPException(status_code=503, detail=str(busy)) from busy

        async def event2_agen():
            async for frame in iter_sync_sse(
                event2_generator,
                label="event2",
                slot_acquired=True,
            ):
                yield frame

        return StreamingResponse(
            event2_agen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
            },
        )

    except HTTPException:
        if cancellation_handle is not None and cancellation_handle.cancelled:
            if cancellation_registry is not None:
                cancellation_registry.finish(cancellation_handle.request_id, "cancelled")
            return _event2_cancelled_response(
                request=request,
                session_store=session_store,
                session_id=session_id,
                analysis_event=locals().get("analysis_event"),
                cancellation_handle=cancellation_handle,
            )
        if cancellation_handle is not None and cancellation_registry is not None:
            cancellation_registry.finish(cancellation_handle.request_id, "failed")
        raise
    except Exception as e:
        # Cancellation is a terminal user action, not an application failure.
        # This branch also covers errors raised before the StreamingResponse was
        # created (for example, while running request preflight checks). Keep
        # the same SSE contract as the generator path and never expose the
        # underlying transport/upstream exception as "处理失败".
        if cancellation_handle is not None and cancellation_handle.cancelled:
            if cancellation_registry is not None:
                cancellation_registry.finish(cancellation_handle.request_id, "cancelled")
            return _event2_cancelled_response(
                request=request,
                session_store=session_store,
                session_id=session_id,
                analysis_event=locals().get("analysis_event"),
                cancellation_handle=cancellation_handle,
            )

        if cancellation_handle is not None and cancellation_registry is not None:
            cancellation_registry.finish(cancellation_handle.request_id, "failed")
        _log_to_file(f"[event2] 处理失败: {e}", "error")
        if session_store is not None:
            try:
                session_store.record_ai_turn(
                    session_id,
                    content=f"处理失败: {str(e)}",
                    metadata={
                        "module_name": "舆情事件分析",
                        "mode": None,
                        "mode_name": "处理异常",
                        "markdown_ready": False,
                        "reports": [],
                        "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                        "knowledge": {"knowledge_base_ids": [], "references": []},
                        "sse": {"thinking_done": False, "result_done": False, "reference_done": True},
                        "errors": [str(e)],
                    },
                )
            except Exception as persist_error:
                _log_to_file(f"[event2] 写入错误会话失败: {persist_error}", "error")
        return AnalysisResponse(
            success=False,
            event_id=event_id_for_error,
            message=f"处理失败: {str(e)}",
        )


@app.post("/api/v1/analysis/event2/stop", tags=["Analysis"])
async def stop_analysis_event2(body: Event2StopRequest):
    """
    停止 event2 流式输出。

    前端契约：只传 session_id。
    request_id / reason 均为后端内部字段（停止按钮固定 reason=user）。
    """
    return await _execute_event2_stop(
        session_id=body.session_id,
        request_id=None,
        reason="user",
    )


@app.post("/api/v1/analysis/event2/cancel/{request_id}", tags=["Analysis"])
async def cancel_analysis_event2(request_id: str):
    """兼容旧路径（按 request_id）；推荐改用 POST /api/v1/analysis/event2/stop + session_id。"""
    return await _execute_event2_stop(
        session_id=None,
        request_id=request_id,
        reason="user",
    )


async def _execute_event2_stop(
    *,
    session_id: Optional[str],
    request_id: Optional[str],
    reason: str,
) -> JSONResponse:
    from singleAgent.cancellation import (
        CrossSessionPreemptionError,
        get_cancellation_registry,
    )
    from singleAgent.pipeline import ensure_cancelled_turn_persisted

    t_begin = time.time()
    sid = (session_id or "").strip()
    rid = (request_id or "").strip()
    if not sid and not rid:
        raise HTTPException(status_code=400, detail="session_id 不能为空")

    from singleAgent.diag_codes import (
        E2101_STOP_BEGIN,
        E2102_STOP_NOT_FOUND,
        E2106_STOP_SLOW,
        E2109_STOP_END,
        E2110_STOP_PERSIST,
        E2301_PERSIST_ERROR,
        E2901_LOOP_BLOCK_SUSPECT,
        format_diag,
    )

    _log_to_file(
        format_diag(
            E2101_STOP_BEGIN,
            "stop_begin",
            session=sid or "-",
            request=rid or "-",
            reason=reason or "user",
        )
    )

    registry = get_cancellation_registry()
    try:
        handle = registry.resolve_for_stop(session_id=sid, request_id=rid)
    except CrossSessionPreemptionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if handle is None:
        _log_to_file(
            format_diag(
                E2102_STOP_NOT_FOUND,
                "stop_not_found",
                session=sid or "-",
                request=rid or "-",
                elapsed_ms=f"{(time.time() - t_begin) * 1000:.1f}",
            )
        )
        return _event2_json_response(
            {
                "request_id": rid,
                "session_id": sid,
                "cancelled": False,
                "status": "not_found",
                "reason": reason or "user",
                "history_saved": False,
                "history_write": "not_found",
                "partial_chars": 0,
            },
            msg="request not running",
        )

    rid = handle.request_id
    t_cancel = time.time()
    from singleAgent.event2_stream import run_in_event2_executor

    result = await run_in_event2_executor(
        registry.request_cancel,
        rid,
        reason or "user",
        timeout=float(os.getenv("EVENT2_STOP_CANCEL_TIMEOUT_SEC", "8") or "8"),
    )
    cancel_ms = (time.time() - t_cancel) * 1000
    _log_to_file(
        format_diag(
            E2109_STOP_END,
            "cancel_phase_done",
            request=rid,
            session=handle.session_id,
            accepted=result.cancelled,
            status=handle.status,
            partial_chars=len(handle.partial_text or ""),
            cancel_ms=f"{cancel_ms:.1f}",
        )
    )

    persist: Dict[str, Any] = {
        "saved": False,
        "already": False,
        "status": "skipped",
    }
    # 仅在真正进入取消流程时等待并兜底落库；已完成的成功回合不可再写成 cancelled
    if result.cancelled or handle.status in ("cancelling", "cancelled"):
        loop = asyncio.get_running_loop()
        # 给流式生成器一点时间自行落库；已落库或终态则立刻返回
        deadline = loop.time() + 0.5
        while (
            handle.status == "cancelling"
            and not handle.terminal
            and not handle.cancel_persisted
            and loop.time() < deadline
        ):
            await asyncio.sleep(0.02)

        t_persist = time.time()
        persist = await run_in_event2_executor(
            ensure_cancelled_turn_persisted,
            handle,
            reason=handle.cancel_reason or reason or "user",
            project_root=PROJECT_ROOT,
            timeout=float(os.getenv("EVENT2_STOP_PERSIST_TIMEOUT_SEC", "8") or "8"),
        )
        persist_ms = (time.time() - t_persist) * 1000
        persist_status = str(persist.get("status") or "")
        persist_code = (
            E2301_PERSIST_ERROR
            if persist_status == "error"
            else E2110_STOP_PERSIST
        )
        _log_to_file(
            format_diag(
                persist_code,
                "persist_done",
                request=rid,
                persist=persist_status,
                persist_ms=f"{persist_ms:.1f}",
                terminal=handle.terminal,
            )
        )
        if not handle.terminal:
            registry.finish(rid, "cancelled")

    history_saved = bool(persist.get("saved") or persist.get("already"))
    partial_chars = len(handle.partial_text or "")
    if persist.get("partial_chars") is not None:
        try:
            partial_chars = int(persist["partial_chars"])
        except (TypeError, ValueError):
            pass

    status = handle.status
    cancelled = bool(result.cancelled or status == "cancelled")
    msg = "stopped" if cancelled else "request not cancellable"
    if history_saved and cancelled:
        msg = "stopped and saved to history"

    total_ms = (time.time() - t_begin) * 1000
    _log_to_file(
        format_diag(
            E2109_STOP_END,
            "stop_end",
            request=rid,
            session=handle.session_id,
            cancelled=cancelled,
            status=status,
            history_saved=history_saved,
            total_ms=f"{total_ms:.1f}",
        )
    )
    if total_ms >= 2000:
        logger.error(
            format_diag(
                E2106_STOP_SLOW,
                "stop_slow",
                request=rid,
                total_ms=f"{total_ms:.1f}",
                cancel_ms=f"{cancel_ms:.1f}",
            )
        )
        if cancel_ms >= 2000:
            logger.error(
                format_diag(
                    E2901_LOOP_BLOCK_SUSPECT,
                    "cancel_phase_also_slow",
                    request=rid,
                    total_ms=f"{total_ms:.1f}",
                    cancel_ms=f"{cancel_ms:.1f}",
                )
            )

    return _event2_json_response(
        {
            "request_id": rid,
            "session_id": handle.session_id,
            "cancelled": cancelled,
            "status": status,
            "reason": handle.cancel_reason or reason or "user",
            "history_saved": history_saved,
            "history_write": persist.get("status") or "",
            "partial_chars": partial_chars,
        },
        msg=msg,
    )


@app.get("/api/v1/analysis/status/{task_id}", response_model=ReportStatus, tags=["Analysis"])
async def get_analysis_status(task_id: str):
    """
    查询分析任务状态
    
    Args:
        task_id: 任务ID
        
    Returns:
        ReportStatus - 当前任务状态，包括各引擎状态
    """
    with tasks_lock:
        task = tasks_registry.get(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    
    engines_status = task.get_engines_status()
    
    rg = task.report_generator
    return ReportStatus(
        task_id=task.task_id,
        status=task.status.value,
        progress=task.progress,
        engines=engines_status,
        report_file=rg.report_file if rg else None,
        ir_file_path=rg.ir_file_path if rg else None,
        pdf_file=rg.pdf_file if rg else None,
        pdf_ready=rg.pdf_ready if rg else False,
        pdf_error=rg.pdf_error if rg else None,
        markdown_file=rg.markdown_file if rg else None,
        markdown_ready=rg.markdown_ready if rg else False,
        markdown_error=rg.markdown_error if rg else None,
        markdown_url=_markdown_url_for(task.task_id) if rg and rg.markdown_ready else None,
        error_reason=task.error_reason,
        error_message=task.error_reason or (
            task.message if task.status in [TaskStatus.ERROR, TaskStatus.CANCELLED] else None
        ),
    )


@app.get("/api/v1/analysis/stream/{task_id}", tags=["Analysis"])
async def stream_analysis_progress(task_id: str, request: Request):
    """
    SSE 流式推送分析流程进度。

    事件类型: task_started, task_progress, engine_progress, workflow,
              report_stage（verbose 调试）, agent_log, task_completed, task_error, heartbeat

    支持 Last-Event-ID 断线续传。非 verbose 不推送 report_stage 的 token delta。
    """
    with tasks_lock:
        task = tasks_registry.get(task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    if _parse_stream_verbose(request, None):
        task.stream_verbose = True
    if _parse_sse_format(request, None) == "decision":
        task.sse_format = "decision"

    last_event_header = request.headers.get("Last-Event-ID")
    try:
        last_event_id = int(last_event_header) if last_event_header else None
    except ValueError:
        last_event_id = None

    return StreamingResponse(
        _sse_generator_for_task(task, last_event_id=last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/v1/analysis/result/{task_id}", tags=["Analysis"])
async def get_analysis_result(task_id: str):
    """
    获取分析结果（完整报告和引擎输出）
    
    Args:
        task_id: 任务ID
        
    Returns:
        完整分析结果
    """
    with tasks_lock:
        task = tasks_registry.get(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    
    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"任务未完成，当前状态: {task.status.value}"
        )
    
    result = task.get_result()
    if not result:
        raise HTTPException(status_code=404, detail="结果不存在")
    
    return JSONResponse(content={
        'success': True,
        **result
    })


@app.get("/api/v1/analysis/report/{task_id}", tags=["Analysis"])
async def get_report_html(task_id: str):
    """
    获取报告HTML内容
    
    Args:
        task_id: 任务ID
        
    Returns:
        HTML报告内容
    """
    with tasks_lock:
        task = tasks_registry.get(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    
    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"任务未完成，当前状态: {task.status.value}"
        )
    
    if not task.report_generator or not task.report_generator.html_content:
        raise HTTPException(status_code=404, detail="报告内容不存在")
    
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=task.report_generator.html_content)


@app.get("/api/v1/analysis/report/{task_id}/markdown", tags=["Analysis"])
async def get_report_markdown(task_id: str):
    """
    下载预生成的 Markdown 报告（默认正式交付物）。

    报告阶段结束后已从 IR 预渲染并保存至 final_reports/。
    """
    with tasks_lock:
        task = tasks_registry.get(task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"任务未完成，当前状态: {task.status.value}"
        )

    rg = task.report_generator
    if not rg:
        raise HTTPException(status_code=404, detail="报告生成器不存在")

    if not rg.markdown_ready or not rg.markdown_file:
        raise HTTPException(
            status_code=404,
            detail=rg.markdown_error or "Markdown 尚未生成",
        )

    md_path = rg._resolve_path(rg.markdown_file)
    if not md_path.exists():
        raise HTTPException(status_code=404, detail=f"Markdown 文件不存在: {rg.markdown_file}")

    return FileResponse(
        path=str(md_path),
        media_type="text/markdown; charset=utf-8",
        filename=md_path.name,
        headers={"Content-Disposition": f'attachment; filename="{md_path.name}"'},
    )


@app.get("/api/v1/analysis/report/{task_id}/pdf", tags=["Analysis"])
async def get_report_pdf(task_id: str):
    """
    下载预生成的 PDF 报告文件（正式交付物）。

    报告阶段结束后已从 IR 预渲染并保存至 final_reports/pdf/，
    本接口直接返回磁盘上的 PDF 文件流。

    Args:
        task_id: 任务ID

    Returns:
        application/pdf 文件流
    """
    with tasks_lock:
        task = tasks_registry.get(task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"任务未完成，当前状态: {task.status.value}"
        )

    rg = task.report_generator
    if not rg:
        raise HTTPException(status_code=404, detail="报告生成器不存在")

    if not rg.pdf_ready or not rg.pdf_file:
        if not ENABLE_PDF_EXPORT:
            raise HTTPException(
                status_code=410,
                detail={
                    "error": "PDF 导出未启用",
                    "message": "请使用 Markdown 交付物，或设置 ENABLE_PDF_EXPORT=true",
                    "markdown_url": _markdown_url_for(task_id),
                },
            )
        if rg.pdf_error and "缺少" in rg.pdf_error:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "PDF 导出功能不可用：缺少系统依赖",
                    "message": rg.pdf_error,
                },
            )
        raise HTTPException(
            status_code=404,
            detail=rg.pdf_error or "PDF 尚未生成",
        )

    pdf_path = rg._resolve_path(rg.pdf_file)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"PDF 文件不存在: {rg.pdf_file}")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=pdf_path.name,
        headers={"Content-Disposition": f'attachment; filename="{pdf_path.name}"'},
    )


@app.post("/api/v1/analysis/cancel/{task_id}", tags=["Analysis"])
async def cancel_analysis(task_id: str):
    """
    取消分析任务
    
    Args:
        task_id: 任务ID
        
    Returns:
        取消结果
    """
    with tasks_lock:
        task = tasks_registry.get(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    
    if task.status in [TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED]:
        return JSONResponse(content={
            'success': False,
            'message': f'任务已结束，无法取消 (状态: {task.status.value})'
        })
    
    # 停止ForumEngine
    if task.forum_executor:
        task.forum_executor.stop()
    
    task.fail_task("用户取消任务", status=TaskStatus.CANCELLED, emit_error_event=False)
    
    return JSONResponse(content={
        'success': True,
        'message': '任务已取消',
        'task_id': task.task_id
    })


@app.get("/api/v1/analysis/tasks", tags=["Analysis"])
async def list_tasks(limit: int = 20, status_filter: Optional[str] = None):
    """
    获取任务列表
    
    Args:
        limit: 返回数量限制
        status_filter: 可选的状态过滤
        
    Returns:
        任务列表
    """
    with tasks_lock:
        tasks = list(tasks_registry.values())
    
    # 按创建时间倒序
    tasks.sort(key=lambda x: x.created_at, reverse=True)
    
    # 应用状态过滤
    if status_filter:
        tasks = [t for t in tasks if t.status.value == status_filter]
    
    return JSONResponse(content={
        'success': True,
        'count': len(tasks),
        'tasks': [t.to_dict() for t in tasks[:limit]]
    })


@app.get("/api/v1/engines/status", tags=["Engines"])
async def get_engines_status():
    """
    获取所有引擎的配置状态（检查是否已配置API密钥）
    
    Returns:
        各引擎的配置状态
    """
    config = load_config()
    
    return JSONResponse(content={
        'success': True,
        'engines': {
            'insight': {
                'available': bool(config.get('INSIGHT_ENGINE_API_KEY')),
                'model': config.get('INSIGHT_ENGINE_MODEL_NAME', '未配置'),
                'has_db_config': bool(config.get('DB_HOST') and config.get('DB_NAME')),
            },
            'media': {
                'available': bool(config.get('MEDIA_ENGINE_API_KEY')),
                'model': config.get('MEDIA_ENGINE_MODEL_NAME', '未配置'),
                'search_tool': config.get('SEARCH_TOOL_TYPE', '未配置'),
            },
            'query': {
                'available': bool(config.get('QUERY_ENGINE_API_KEY') and config.get('TAVILY_API_KEY')),
                'model': config.get('QUERY_ENGINE_MODEL_NAME', '未配置'),
            },
            'forum': {
                'available': True,  # ForumEngine不依赖API密钥
            },
            'report': {
                'available': bool(config.get('REPORT_ENGINE_API_KEY')),
                'model': config.get('REPORT_ENGINE_MODEL_NAME', '未配置'),
            }
        }
    })


@app.get("/api/v1/analysis/event2/sessions", tags=["singleAgent"])
async def list_event2_sessions(page_no: int = 1, page_size: int = 20, agent_id: Optional[int] = 5):
    store = _get_event2_session_store()
    data = store.list_sessions(agent_id=agent_id, page_no=page_no, page_size=page_size)
    return _event2_json_response(data)


@app.get("/api/v1/analysis/event2/sessions/{session_id}", tags=["singleAgent"])
async def get_event2_session(session_id: str, agent_id: Optional[int] = 5):
    store = _get_event2_session_store()
    session = store.get(session_id)
    if agent_id is not None and session.agent_id not in (None, agent_id):
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    detail = session.to_detail()
    if session.pending_template is not None:
        from singleAgent.report_template_loader import load_all_report_templates

        detail["available_templates"] = [
            template.to_knowledge_ref()
            for template in load_all_report_templates(
                project_root=PROJECT_ROOT, session_id=session_id
            )
        ]
    return _event2_json_response(detail)


if __name__ == "__main__":
    import uvicorn

    _uv_level = (os.getenv("UVICORN_LOG_LEVEL") or _LOG_LEVEL or "warning").lower()
    uvicorn.run(
        "fastapi_integration:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,  # 禁用热重载，确保代码完全重新加载
        log_level=_uv_level,
    )
