"""event2 修改定稿模式（mode=2）：无检索，流式输出完整 Markdown 报告。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from loguru import logger

from singleAgent.cancellation import GenerationCancelled, GenerationHandle
from singleAgent.common import pace, save_markdown, yield_progress
from singleAgent.config import (
    LLM_MAX_TOKENS,
    PHASE_GAP_DELAY_SEC,
    PIPELINE_SOFT_TIMEOUT_SEC,
    PRE_LLM_DELAY_SEC,
    REPORT_LLM_TEMPERATURE,
    REPORT_LLM_TIMEOUT_SEC,
    REPORT_INDEX,
    RESULT_CHUNK_DELAY_SEC,
)
from singleAgent.llm import resolve_llm_settings, stream_chat
from singleAgent.prompts import (
    REVISE_MODE_ADDON,
    build_revise_system_prompt,
    build_revise_user_prompt,
    build_system_prompt,
)
from singleAgent.report_template_loader import (
    load_report_template,
    resolve_template_id,
    use_report_template,
)
from singleAgent.session_store import SessionState, SessionStore
from singleAgent.result_stream import ResultStreamClassifier, StreamPiece, extract_body_markdown
from singleAgent.sse import (
    emit_ping,
    emit_reference_done,
    emit_result_chunk,
    emit_result_end,
    emit_result_start,
    emit_result_typed,
    emit_retrieval_knowledge,
    emit_thinking_done,
)


def run_revise_pipeline(
    event: Any,
    config: Dict[str, Any],
    *,
    llm_override: Optional[Dict[str, str]] = None,
    project_root: Optional[Path] = None,
    session_store: SessionStore,
    session: SessionState,
    session_id: str,
    instruction: str,
    started: float,
    cancellation: Optional[GenerationHandle] = None,
) -> Generator[str, None, None]:
    root = project_root or Path(__file__).resolve().parent.parent
    topic = session.last_topic or event.topic or event.query or "分析报告"
    meta = event.metadata or {}
    md_rel_path = ""
    result_started = False
    template = None

    def timed_out() -> bool:
        return (time.time() - started) >= PIPELINE_SOFT_TIMEOUT_SEC

    def check_cancelled() -> None:
        if cancellation:
            cancellation.raise_if_cancelled()

    try:
        check_cancelled()
        if not session.has_report:
            yield from yield_progress("当前会话尚无报告，将按新分析处理…")
            from singleAgent.report_pipeline import run_report_pipeline

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
            return

        yield from yield_progress(f"将基于「{topic}」生成修订版完整回答...")
        if use_report_template(meta):
            template = load_report_template(
                resolve_template_id(meta),
                project_root=root,
                session_id=session_id,
            )
            check_cancelled()
            yield emit_retrieval_knowledge(
                refs=[template.to_knowledge_ref()],
                message=f"已选报告版式：{template.name}",
                session_id=session_id,
                progress=10,
            )
        yield emit_thinking_done("思考结束")

        llm_cfg = resolve_llm_settings(config, llm_override)
        if use_report_template(meta):
            system_prompt = build_system_prompt(
                topic=topic,
                metadata=meta,
                project_root=root,
            ) + "\n\n" + REVISE_MODE_ADDON
        else:
            system_prompt = build_revise_system_prompt()
        user_prompt = build_revise_user_prompt(
            instruction=instruction,
            topic=topic,
            last_report_md=session.last_report_md,
            last_search_summary=session.last_search_summary,
            recent_messages=session.messages,
        )

        yield from yield_progress(
            f"正在生成修订版报告（{llm_cfg['model_name']}）...",
        )
        pace(PRE_LLM_DELAY_SEC)
        check_cancelled()

        markdown_parts: list[str] = []
        body_parts: list[str] = []
        classifier = ResultStreamClassifier()
        last_ping = time.time()
        last_chunk_emit = 0.0

        def _emit_pieces(pieces: list[StreamPiece]) -> Generator[str, None, None]:
            nonlocal result_started, last_ping, last_chunk_emit
            for piece in pieces:
                check_cancelled()
                if not piece.text:
                    continue
                markdown_parts.append(piece.text)
                if piece.stream_type == "chunk":
                    body_parts.append(piece.text)
                if not result_started:
                    yield emit_result_start(title=topic, file_path="")
                    result_started = True
                if time.time() - last_ping > 15:
                    yield emit_ping()
                    last_ping = time.time()
                now = time.time()
                wait = RESULT_CHUNK_DELAY_SEC - (now - last_chunk_emit)
                if wait > 0:
                    time.sleep(wait)
                check_cancelled()
                if cancellation:
                    cancellation.append_partial(piece.text)
                yield emit_result_typed(piece.text, piece.stream_type, piece.aux_kind)
                last_chunk_emit = time.time()

        for delta in stream_chat(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            model_name=llm_cfg["model_name"],
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=(
                min(template.max_output_tokens, LLM_MAX_TOKENS)
                if template is not None
                else LLM_MAX_TOKENS
            ),
            temperature=(
                REPORT_LLM_TEMPERATURE if template is not None else None
            ),
            timeout=(
                REPORT_LLM_TIMEOUT_SEC if template is not None else None
            ),
            check_connectivity=False,
            cancellation=cancellation,
        ):
            check_cancelled()
            if timed_out():
                if not result_started:
                    yield emit_result_start(title=topic, file_path="")
                    result_started = True
                tail = "\n\n> （已达快模式时间上限，输出在此截断）"
                markdown_parts.append(tail)
                body_parts.append(tail)
                if cancellation:
                    cancellation.append_partial(tail)
                yield emit_result_chunk(tail)
                break

            yield from _emit_pieces(classifier.feed(delta))

        yield from _emit_pieces(classifier.flush())

        full_md = "".join(markdown_parts).strip()
        body_md = "".join(body_parts).strip() or extract_body_markdown(full_md)
        if not full_md and result_started:
            yield emit_result_chunk("> 模型未返回正文，请检查 LLM 配置与连通性。")

        if full_md:
            check_cancelled()
            md_rel_path = save_markdown(topic, full_md, root)
            check_cancelled()
            session_store.save_report(session_id, topic, body_md)
            if instruction:
                session_store.append_message(session_id, "user", instruction)
            session_store.append_message(session_id, "assistant", body_md)

        check_cancelled()
        try:
            session_store.record_ai_turn(
                session_id,
                # 平台历史回显读 content；须写报告正文，不能只写占位短句
                content=(
                    body_md
                    if body_md
                    else "模型未返回正文，请检查 LLM 配置与连通性。"
                ),
                metadata={
                    "module_name": "舆情事件分析",
                    "request_id": meta.get("request_id") or "",
                    "source_request_id": meta.get("source_request_id") or "",
                    "turn_status": "completed",
                    "cancelled": False,
                    "mode": 2,
                    "mode_name": "修改定稿",
                    "markdown_ready": bool(body_md),
                    "reports": [
                        {
                            "index": REPORT_INDEX,
                            "file_path": md_rel_path,
                            "report_text": full_md,
                        }
                    ] if full_md else [],
                    "search": {
                        "terms_tried": [],
                        "hit_count": 0,
                        "summary": session.last_search_summary,
                    },
                    "knowledge": {
                        "knowledge_base_ids": session.last_kb_ids,
                        "references": [],
                        "summary": session.last_kb_summary,
                    },
                    "sse": {
                        "thinking_done": True,
                        "result_done": result_started,
                        "reference_done": True,
                    },
                    "errors": [] if body_md else ["模型未返回正文"],
                },
            )
        except Exception as persist_error:
            logger.warning(f"写入 event2 修订会话失败: {persist_error}")

        if result_started:
            yield emit_result_end(file_path=md_rel_path)

        yield emit_thinking_done(
            success=bool(body_md),
            reports=[{"index": REPORT_INDEX, "file_path": md_rel_path}],
            markdown_ready=bool(body_md),
            message="" if body_md else "模型未返回正文",
            mode=2,
        )
        yield emit_reference_done()

    except GenerationCancelled:
        raise
    except Exception as e:
        logger.exception(f"修改定稿模式失败: {e}")
        try:
            session_store.record_ai_turn(
                session_id,
                content=f"修订失败：{e}",
                metadata={
                    "module_name": "舆情事件分析",
                    "request_id": meta.get("request_id") or "",
                    "source_request_id": meta.get("source_request_id") or "",
                    "turn_status": "failed",
                    "cancelled": False,
                    "mode": 2,
                    "mode_name": "修改定稿",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {
                        "terms_tried": [],
                        "hit_count": 0,
                        "summary": session.last_search_summary,
                    },
                    "knowledge": {
                        "knowledge_base_ids": session.last_kb_ids,
                        "references": [],
                        "summary": session.last_kb_summary,
                    },
                    "sse": {
                        "thinking_done": False,
                        "result_done": result_started,
                        "reference_done": True,
                    },
                    "errors": [str(e)],
                },
            )
        except Exception as persist_error:
            logger.warning(f"写入 event2 修订错误会话失败: {persist_error}")
        yield from yield_progress(f"修订失败：{e}")
        if result_started:
            yield emit_result_end(file_path="")
        yield emit_thinking_done(success=False, message=str(e), mode=2)
        yield emit_reference_done()
