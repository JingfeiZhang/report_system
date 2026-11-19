"""
singleAgent 主编排。

产品规则：
- ext 为空 → mode1 自由讨论
- 已有前序回合且 ext 选择模板/材料 → mode0 正式报告
- template_generation 已校验待选内容 → mode0 正式报告
- 首轮模板选择只用于展示，不直接生成报告
- 附件 content 在入口合并进 instruction，不参与 mode 判断
- ismodel=1 且未选模板：只推右栏模板 + 短提示（无 CoT）
- ismodel=2：正常讨论，不推模板
- ismodel 与 ext 无关：不因 ext 覆盖 ismodel
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from loguru import logger

from singleAgent.cancellation import GenerationCancelled, GenerationHandle
from singleAgent.chat_pipeline import run_chat_pipeline
from singleAgent.common import yield_progress
from singleAgent.ext_utils import ext_has_template_selection, ext_is_nonempty
from singleAgent.report_pipeline import run_report_pipeline
from singleAgent.router import RouteDecision
from singleAgent.session_store import SessionState, get_session_store
from singleAgent.sse import emit_cancelled, emit_reference_done, emit_thinking_done


def _emit_failure_reference() -> Generator[str, None, None]:
    yield emit_thinking_done(success=False, message="处理失败")
    yield emit_reference_done()


def _emit_cancelled_response(
    session_store: Any,
    session_id: str,
    event: Any,
    instruction: str,
    metadata: Dict[str, Any],
    request_id: str,
    cancellation: Optional[GenerationHandle],
) -> Generator[str, None, None]:
    reason = cancellation.cancel_reason if cancellation else "user"
    _persist_cancelled_turn(
        session_store,
        session_id,
        event,
        instruction,
        metadata,
        request_id,
        cancellation,
        reason=reason,
        fallback_content="已停止输出。",
        reference_done=True,
    )
    yield emit_cancelled(
        request_id=request_id,
        session_id=session_id,
        reason=reason,
    )
    yield emit_reference_done()


def _has_previous_turn(session: SessionState, request_id: str = "") -> bool:
    """Return whether the session has a turn preceding the current request."""
    for item in session.conversations or []:
        role = (getattr(item, "role", None) or "").strip().lower()
        metadata = getattr(item, "metadata", None)
        item_request_id = metadata.get("request_id") if isinstance(metadata, dict) else ""
        if role in ("human", "user", "ai", "assistant") and item_request_id != request_id:
            return True
    if session.conversations:
        return False
    for msg in session.messages or []:
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") or "").strip().lower() in ("human", "user", "assistant", "ai"):
            return True
    return bool(session.has_report)


def _persist_cancelled_turn(
    session_store: Any,
    session_id: str,
    event: Any,
    instruction: str,
    metadata: Dict[str, Any],
    request_id: str,
    cancellation: Optional[GenerationHandle],
    *,
    reason: str,
    fallback_content: str,
    reference_done: bool,
) -> None:
    """Persist one cancelled turn, guarding against duplicate disconnect callbacks."""
    if cancellation is not None and not cancellation.mark_cancel_persisted():
        return

    try:
        event_metadata = event.metadata if isinstance(event.metadata, dict) else {}
        session_store.record_ai_turn(
            session_id,
            content=(cancellation.partial_text if cancellation else "") or fallback_content,
            metadata={
                "module_name": "舆情事件分析",
                "mode": None,
                "mode_name": "已取消",
                "request_id": request_id,
                "source_request_id": metadata.get("source_request_id") or "",
                "resolved_topic": event_metadata.get("resolved_topic") or "",
                "topic_source": event_metadata.get("topic_source") or "",
                "turn_status": "cancelled",
                "cancelled": True,
                "cancel_reason": reason,
                "markdown_ready": False,
                "reports": [],
                "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                "knowledge": {"knowledge_base_ids": [], "references": [], "summary": ""},
                "sse": {
                    "thinking_done": False,
                    "result_done": False,
                    "reference_done": reference_done,
                },
                "errors": [],
            },
        )
    except Exception as persist_error:
        logger.warning(f"写入 event2 取消会话失败: {persist_error}")


def ensure_cancelled_turn_persisted(
    cancellation: Optional[GenerationHandle],
    *,
    reason: str = "user",
    fallback_content: str = "已停止输出。",
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    停止接口兜底：若流式侧尚未落库，则把已生成 partial 写入会话/平台历史。
    与 `_persist_cancelled_turn` 共用 mark_cancel_persisted，避免重复写入。
    """
    if cancellation is None:
        return {"saved": False, "already": False, "status": "no_handle"}
    if not cancellation.mark_cancel_persisted():
        return {"saved": False, "already": True, "status": "already"}

    content = (cancellation.partial_text or "").strip() or fallback_content
    try:
        root = project_root or Path(__file__).resolve().parent.parent
        session_store = get_session_store(root)
        session_store.record_ai_turn(
            cancellation.session_id,
            content=content,
            metadata={
                "module_name": "舆情事件分析",
                "mode": None,
                "mode_name": "已取消",
                "request_id": cancellation.request_id,
                "source_request_id": "",
                "resolved_topic": "",
                "topic_source": "",
                "turn_status": "cancelled",
                "cancelled": True,
                "cancel_reason": reason or cancellation.cancel_reason or "user",
                "markdown_ready": False,
                "reports": [],
                "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                "knowledge": {"knowledge_base_ids": [], "references": [], "summary": ""},
                "sse": {
                    "thinking_done": False,
                    "result_done": False,
                    "reference_done": True,
                },
                "errors": [],
                "persisted_by": "stop_api",
            },
        )
        return {
            "saved": True,
            "already": False,
            "status": "written",
            "partial_chars": len(content),
        }
    except Exception as persist_error:
        logger.warning(
            f"停止接口写入取消会话失败 request={cancellation.request_id}: {persist_error}"
        )
        return {"saved": False, "already": False, "status": "error"}


def _decide_mode_by_ext(
    ext: Dict[str, Any],
    *,
    first_round: bool,
    has_previous_turn: Optional[bool] = None,
    template_generation: bool = False,
) -> RouteDecision:
    previous = (not first_round) if has_previous_turn is None else has_previous_turn
    has_template = ext_has_template_selection(ext)
    if has_template and template_generation:
        return RouteDecision(
            mode=0,
            reason="已校验待选内容并选择报告模板",
            source="guardrail",
        )
    if has_template and previous:
        return RouteDecision(
            mode=0,
            reason="已存在前序对话并选择报告模板",
            source="guardrail",
        )
    if has_template and not previous:
        return RouteDecision(
            mode=1,
            reason="首轮不能直接使用模板生成报告",
            source="guardrail",
        )
    if first_round:
        return RouteDecision(
            mode=1,
            reason="首轮讨论：ext 强制为空",
            source="guardrail",
        )
    if ext_is_nonempty(ext):
        return RouteDecision(
            mode=0,
            reason="ext 非空",
            source="guardrail",
        )
    return RouteDecision(
        mode=1,
        reason="ext 为空，自由讨论",
        source="guardrail",
    )


def run_fast_analysis(
    event: Any,
    config: Dict[str, Any],
    llm_override: Optional[Dict[str, str]] = None,
    project_root: Optional[Path] = None,
    cancellation: Optional[GenerationHandle] = None,
) -> Generator[str, None, None]:
    """
    快模式 SSE 生成器。event 为 AnalysisEvent（topic/query/metadata）。
    0=正式报告（ext 非空），1=自由讨论（ext 空）。
    附件 content 在入口合并进 instruction，不参与 mode 判断。
    """
    started = time.time()
    root = project_root or Path(__file__).resolve().parent.parent
    meta = dict(event.metadata or {})
    session_id = (meta.get("session_id") or event.event_id or "default").strip() or "default"

    # 入口：user_prompt += attachment content（与 instruction 等价）
    from singleAgent.ext_utils import apply_attachment_merge_to_metadata

    instruction = apply_attachment_merge_to_metadata(meta)
    if not instruction:
        instruction = (event.query or event.topic or "").strip()
        if instruction:
            meta["instruction"] = instruction

    ext = meta.get("ext") if isinstance(meta.get("ext"), dict) else {}
    files = meta.get("files_data") if isinstance(meta.get("files_data"), list) else []
    request_id = (meta.get("request_id") or "").strip()
    interaction_stage = str(meta.get("interaction_stage") or "").strip()
    template_generation = interaction_stage == "template_generation"
    route_ext = ext

    session_store = get_session_store(root)
    session = session_store.get(session_id)

    has_previous_turn = _has_previous_turn(session, request_id=request_id)
    first_round = not has_previous_turn
    if first_round and not template_generation:
        # 保留首轮门禁：模板选择不直接生成报告；ismodel 仍由前端独立控制模板推送。
        ext = {}
        meta["ext"] = {}

    # ismodel 与 ext 无关：只规范化前端/上游已写入的值
    try:
        ismodel = int(meta.get("ismodel", 2))
    except (TypeError, ValueError):
        ismodel = 2
    meta["ismodel"] = ismodel if ismodel in (1, 2) else 2

    # 回写，供下游 pipeline / 模板解析读取
    event.metadata = meta

    try:
        if cancellation:
            cancellation.raise_if_cancelled()
        yield from yield_progress("收到请求...")
        decision = _decide_mode_by_ext(
            route_ext,
            first_round=first_round,
            has_previous_turn=has_previous_turn,
            template_generation=template_generation,
        )
        logger.info(
            f"event2 route session={session_id} mode={decision.mode} "
            f"source={decision.source} reason={decision.reason} "
            f"request={request_id} first_round={first_round} "
            f"interaction_stage={interaction_stage or 'default'} "
            f"ext_nonempty={ext_is_nonempty(route_ext)} "
            f"files={len(files)} instruction_chars={len(instruction)}"
        )

        if decision.mode == 0:
            yield from yield_progress(f"将为您生成分析报告（{decision.reason}）")
            yield from run_report_pipeline(
                event,
                config,
                llm_override=llm_override,
                project_root=root,
                session_store=session_store,
                session_id=session_id,
                started=started,
                cancellation=cancellation,
            )
        else:
            yield from yield_progress(f"对话模式（{decision.reason}）")
            yield from run_chat_pipeline(
                event,
                config,
                llm_override=llm_override,
                project_root=root,
                session_store=session_store,
                session=session,
                session_id=session_id,
                instruction=instruction,
                started=started,
                cancellation=cancellation,
            )

    except GenerationCancelled:
        yield from _emit_cancelled_response(
            session_store,
            session_id,
            event,
            instruction,
            meta,
            request_id,
            cancellation,
        )
    except GeneratorExit:
        if cancellation:
            cancellation.request_cancel("disconnect", join_closers=False)
            _persist_cancelled_turn(
                session_store,
                session_id,
                event,
                instruction,
                meta,
                request_id,
                cancellation,
                reason="disconnect",
                fallback_content="连接中断，已停止输出。",
                reference_done=False,
            )
        raise
    except Exception as e:
        if cancellation is not None and cancellation.cancelled:
            yield from _emit_cancelled_response(
                session_store,
                session_id,
                event,
                instruction,
                meta,
                request_id,
                cancellation,
            )
            return
        logger.exception(f"singleAgent 失败: {e}")
        try:
            session_store.record_ai_turn(
                session_id,
                content=f"处理失败：{e}",
                metadata={
                    "module_name": "舆情事件分析",
                    "mode": None,
                    "mode_name": "处理异常",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {
                        "terms_tried": [],
                        "hit_count": 0,
                        "summary": "",
                    },
                    "knowledge": {
                        "knowledge_base_ids": [],
                        "references": [],
                        "summary": "",
                    },
                    "sse": {
                        "thinking_done": False,
                        "result_done": False,
                        "reference_done": True,
                    },
                    "errors": [str(e)],
                },
            )
        except Exception as persist_error:
            logger.warning(f"写入 singleAgent 错误会话失败: {persist_error}")
        yield from yield_progress(f"处理失败：{e}")
        yield from _emit_failure_reference()
