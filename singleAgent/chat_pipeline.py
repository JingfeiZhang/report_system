"""event2 对话模式（mode=1）：轻量达梦/知识库检索 + 讨论回复。

ismodel=1 且尚未勾选模板：只推右栏模板 + 短提示，不跑检索/CoT/LLM。
ismodel=2：正常讨论，不推模板。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from loguru import logger

from singleAgent.cancellation import GenerationCancelled, GenerationHandle
from singleAgent.common import pace, yield_progress, yield_thinking_summary
from singleAgent.config import (
    CHAT_KB_MAX_REFS,
    CHAT_LLM_MAX_TOKENS,
    CHAT_LLM_TEMPERATURE,
    CHAT_MAX_SEARCH_TERMS,
    CHAT_PROGRESS_DELAY_SEC,
    CHAT_RESULT_CHUNK_DELAY_SEC,
    CHAT_SEARCH_PLAN_ROUNDS,
    CHAT_TARGET_HITS,
    PHASE_GAP_DELAY_SEC,
    PIPELINE_SOFT_TIMEOUT_SEC,
    REPORT_INDEX,
    SEARCH_ROUND_DELAY_SEC,
)
from singleAgent.citations import (
    CitationTipEmitter,
    build_citation_tips,
    format_citation_catalog,
    remap_citation_markers_by_appearance,
)
from singleAgent.event2_retrieval import (
    is_event2_agent_retrieval,
    is_event2_mcp_retrieval,
    retrieve_event2_context,
)
from singleAgent.dm_search import SearchBundle
from singleAgent.ext_utils import ext_has_template_selection
from singleAgent.knowledge_base import KnowledgeBundle, retrieve as retrieve_knowledge
from singleAgent.llm import resolve_llm_settings, stream_chat
from singleAgent.prompts import build_chat_system_prompt, build_chat_user_prompt
from singleAgent.report_template_loader import load_all_report_templates, resolve_ismodel
from singleAgent.search_planner import build_initial_search_plan, normalize_topic
from singleAgent.session_store import (
    SessionState,
    SessionStore,
    build_recent_conversation_context,
)
from singleAgent.sse import (
    emit_ping,
    emit_reference_done,
    emit_result_chunk,
    emit_result_end,
    emit_result_start,
    emit_result_typed,
    emit_retrieval_knowledge,
    emit_thinking_done,
    emit_thinking_section_end,
    emit_thinking_section_start,
)

# 甲方：未选模板时的提示。走 result/awaiting_template（非正文 chunk），文案仍用 Markdown 引用
_AWAITING_TEMPLATE_HINT = (
    "> 请选择右侧模板后发送，即可按所选版式生成报告。"
)
_AWAITING_TEMPLATE_RESULT_TYPE = "awaiting_template"


def _parse_kb_ids(meta: Dict[str, Any]) -> List[str]:
    from singleAgent.knowledge_base import normalize_knowledge_base_ids

    kb_ids = meta.get("knowledge_base_ids") or []
    if isinstance(kb_ids, str):
        try:
            kb_ids = json.loads(kb_ids)
        except (TypeError, ValueError):
            kb_ids = []
    if not isinstance(kb_ids, list):
        return []
    return normalize_knowledge_base_ids(kb_ids)


def _stream_await_template_selection(
    *,
    session_store: SessionStore,
    session_id: str,
    instruction: str,
    topic: str,
    meta: Dict[str, Any],
    project_root: Path,
    cancellation: Optional[GenerationHandle],
) -> Generator[str, None, None]:
    """ismodel=1 且未选模板：推全部模板 + 短提示，跳过检索与 LLM。"""
    request_id = str(meta.get("request_id") or "").strip()
    template_refs: list = []

    def check_cancelled() -> None:
        if cancellation:
            cancellation.raise_if_cancelled()

    check_cancelled()
    # 先吐 ping，避免卫鼎模板 HTTP 卡住时前端一直「...」且连接无首包
    yield emit_ping()
    check_cancelled()
    try:
        templates = load_all_report_templates(
            project_root=project_root, session_id=session_id
        )
        check_cancelled()
        if templates:
            template_refs = [t.to_knowledge_ref() for t in templates]
            yield emit_retrieval_knowledge(
                refs=template_refs,
                message=(
                    f"可选报告版式（共 {len(templates)} 个），请在右侧勾选后发送"
                ),
                session_id=session_id,
                progress=len(templates),
            )
    except Exception as te:
        logger.warning(f"推送全部模板失败: {te}")

    check_cancelled()
    # 不发 thinking section / 检索进度，避免前端展示 CoT
    yield emit_thinking_done(
        "请选择模板",
        success=True,
        mode=1,
        awaiting_template=True,
        interaction_stage="awaiting_template",
    )

    reply = _AWAITING_TEMPLATE_HINT
    yield emit_result_start(title=topic or "请选择模板", file_path="")
    if cancellation:
        cancellation.append_partial(reply)
    yield emit_result_typed(reply, _AWAITING_TEMPLATE_RESULT_TYPE)
    yield emit_result_end(file_path="")

    check_cancelled()
    if instruction:
        session_store.append_message(session_id, "user", instruction)
    session_store.append_message(session_id, "assistant", reply)
    try:
        session_store.record_ai_turn(
            session_id,
            content=reply,
            metadata={
                "module_name": "舆情事件分析",
                "mode": 1,
                "mode_name": "等待选择模板",
                "request_id": request_id,
                "source_request_id": meta.get("source_request_id") or "",
                "resolved_topic": meta.get("resolved_topic") or "",
                "topic_source": meta.get("topic_source") or "",
                "turn_status": "awaiting_template",
                "awaiting_template": True,
                "markdown_ready": False,
                "reports": [],
                "search": {"terms_tried": [], "hit_count": 0, "summary": ""},
                "knowledge": {
                    "knowledge_base_ids": [],
                    "references": template_refs,
                    "summary": (
                        f"已推送 {len(template_refs)} 个报告版式模板"
                        if template_refs
                        else ""
                    ),
                },
                "sse": {
                    "thinking_done": True,
                    "result_done": True,
                    "reference_done": True,
                },
                "errors": [],
            },
        )
    except Exception as persist_error:
        logger.warning(f"写入等待选模板会话失败: {persist_error}")

    yield emit_reference_done()


def run_chat_pipeline(
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
    topic = session.last_topic or event.topic or event.query or "对话"
    result_started = False
    root = project_root or Path(__file__).resolve().parent.parent
    meta = event.metadata if isinstance(event.metadata, dict) else {}
    bundle = SearchBundle()
    kb_bundle = KnowledgeBundle()
    kb_ids: List[str] = []
    template_refs: list = []
    knowledge_refs: list = []
    request_id = str(meta.get("request_id") or "").strip()
    resolved_topic = ""
    topic_source = ""
    ext = meta.get("ext") if isinstance(meta.get("ext"), dict) else {}

    def timed_out() -> bool:
        return (time.time() - started) >= PIPELINE_SOFT_TIMEOUT_SEC

    def check_cancelled() -> None:
        if cancellation:
            cancellation.raise_if_cancelled()

    try:
        check_cancelled()
        # 甲方：ismodel=1 且未选具体模板 → 只提示选模板，不输出 CoT / 不跑 LLM
        if resolve_ismodel(meta) == 1 and not ext_has_template_selection(ext):
            logger.info(
                f"event2 await-template session={session_id} request={request_id} "
                "ismodel=1 ext无模板选择，跳过检索与讨论"
            )
            yield from _stream_await_template_selection(
                session_store=session_store,
                session_id=session_id,
                instruction=instruction,
                topic=topic,
                meta=meta,
                project_root=root,
                cancellation=cancellation,
            )
            return

        yield from yield_progress("正在理解您的问题...", delay=CHAT_PROGRESS_DELAY_SEC)

        llm_cfg = resolve_llm_settings(config, llm_override)
        recent_conversation = build_recent_conversation_context(
            session,
            exclude_request_id=request_id,
        )

        # —— 检索：MCP 达梦直读 或 legacy 舆情库 ——
        check_cancelled()
        if is_event2_mcp_retrieval():
            yield from yield_progress(
                "正在理解讨论主题...",
                delay=CHAT_PROGRESS_DELAY_SEC,
            )
        else:
            yield from yield_progress(
                "正在规划讨论检索词...",
                delay=CHAT_PROGRESS_DELAY_SEC,
            )
        plan = build_initial_search_plan(
            instruction=instruction,
            session_topic=normalize_topic(session.last_topic),
            llm_cfg=llm_cfg,
            max_terms=CHAT_MAX_SEARCH_TERMS,
            conversation_context=recent_conversation,
        )
        check_cancelled()
        topic = plan.topic or topic
        resolved_topic = plan.topic
        topic_source = plan.topic_source
        meta["resolved_topic"] = resolved_topic
        meta["topic_source"] = topic_source
        event.metadata = meta
        session_store.update_last_topic_if_current(
            session_id,
            resolved_topic,
            request_id=request_id,
        )
        logger.info(
            f"event2 topic resolved session={session_id} request={request_id} "
            f"topic={resolved_topic} source={topic_source}"
        )
        terms = list(plan.terms)[:CHAT_MAX_SEARCH_TERMS]

        yield from yield_progress(
            f"认定主题：{topic}"
            + ("" if plan.topic_source == "llm" else f"（{plan.topic_source}）"),
            delay=CHAT_PROGRESS_DELAY_SEC,
        )

        if is_event2_mcp_retrieval():
            check_cancelled()
            pace(SEARCH_ROUND_DELAY_SEC)
            if is_event2_agent_retrieval():
                yield from yield_progress(
                    "TROOPS Agent：正在根据表注释选表并查询达梦库...",
                    delay=CHAT_PROGRESS_DELAY_SEC,
                )
            else:
                yield from yield_progress(
                    "正在通过 MCP 读取达梦业务库...",
                    delay=CHAT_PROGRESS_DELAY_SEC,
                )
            check_cancelled()
            bundle = retrieve_event2_context(
                topic=topic,
                search_text=instruction,
                target_hits=CHAT_TARGET_HITS,
                llm_cfg=llm_cfg,
                cancel_check=check_cancelled,
            )
            check_cancelled()
            if bundle.empty:
                yield from yield_progress(
                    bundle.summary_text or "达梦库未返回可用上下文，将主要依据对话作答",
                    delay=CHAT_PROGRESS_DELAY_SEC,
                )
            else:
                label = (
                    "Agent 已加载"
                    if is_event2_agent_retrieval()
                    else "达梦库已加载"
                )
                yield from yield_progress(
                    f"{label} {bundle.total_count} 条上下文片段",
                    delay=CHAT_PROGRESS_DELAY_SEC,
                )
        elif terms:
            yield from yield_progress(
                f"模型拟定检索词：{'、'.join(terms)}"
                + ("" if plan.terms_source == "llm" else f"（{plan.terms_source}）"),
                delay=CHAT_PROGRESS_DELAY_SEC,
            )
            from singleAgent.dm_search import (
                _open_search_db,
                finalize_search_bundle,
                search_one_term,
            )

            merged: List[Any] = []
            seen_keys: Set[Tuple[str, str, str]] = set()
            tried: List[str] = []
            plan_rounds = max(1, CHAT_SEARCH_PLAN_ROUNDS)
            db = _open_search_db()
            if db is None:
                bundle = SearchBundle(
                    terms_tried=terms,
                    empty=True,
                    summary_text="数据库检索模块不可用。",
                )
            else:
                for _round_idx in range(plan_rounds):
                    if timed_out() or len(merged) >= CHAT_TARGET_HITS:
                        break
                    for term in terms:
                        check_cancelled()
                        if timed_out() or len(merged) >= CHAT_TARGET_HITS:
                            break
                        if term in tried:
                            continue
                        pace(SEARCH_ROUND_DELAY_SEC)
                        check_cancelled()
                        tried.append(term)
                        search_one_term(
                            db,
                            term,
                            merged,
                            seen_keys,
                            target_hits=CHAT_TARGET_HITS,
                        )
                        check_cancelled()
                        yield from yield_progress(
                            f"已完成检索：「{term}」",
                            delay=CHAT_PROGRESS_DELAY_SEC,
                        )
                bundle = finalize_search_bundle(tried, merged)
        else:
            from singleAgent.dm_search import finalize_search_bundle

            bundle = finalize_search_bundle([], [])

        if bundle.empty and not is_event2_mcp_retrieval():
            yield from yield_progress(
                "库内暂无匹配数据，将主要依据对话上下文回答",
                delay=CHAT_PROGRESS_DELAY_SEC,
            )
        elif not bundle.empty:
            yield from yield_progress(
                f"库内共命中 {bundle.total_count} 条，正在整理检索摘要..."
                if not is_event2_mcp_retrieval()
                else f"正在整理达梦库上下文摘要（{bundle.total_count} 条）...",
                delay=CHAT_PROGRESS_DELAY_SEC,
            )
            pace(PHASE_GAP_DELAY_SEC)
            yield emit_thinking_section_start("检索摘要", stage="search")
            yield from yield_thinking_summary(bundle.summary_text, stage="search")
            yield emit_thinking_section_end(stage="search")

        kb_ids = _parse_kb_ids(meta)
        if kb_ids and not timed_out():
            check_cancelled()
            yield from yield_progress(
                f"计划检索知识库：{'、'.join(str(i) for i in kb_ids)}",
                delay=CHAT_PROGRESS_DELAY_SEC,
            )
            pace(PHASE_GAP_DELAY_SEC)
            check_cancelled()
            kb_bundle = retrieve_knowledge(topic or instruction, kb_ids)
            check_cancelled()
            if kb_bundle.chunks and len(kb_bundle.chunks) > CHAT_KB_MAX_REFS:
                kb_bundle.chunks = kb_bundle.chunks[:CHAT_KB_MAX_REFS]
                # 保持 empty / count 语义
                kb_bundle.empty = not kb_bundle.chunks
            if kb_bundle.empty:
                yield from yield_progress(
                    "知识库暂无匹配片段",
                    delay=CHAT_PROGRESS_DELAY_SEC,
                )
            else:
                knowledge_refs = kb_bundle.to_knowledge_refs()
                yield from yield_progress(
                    f"知识库共命中 {kb_bundle.total_count} 条（讨论态已截断）",
                    delay=CHAT_PROGRESS_DELAY_SEC,
                )
                yield emit_retrieval_knowledge(
                    refs=knowledge_refs,
                    message=f"知识库检索完成: {len(knowledge_refs)} 条",
                    session_id=session_id,
                    progress=len(knowledge_refs),
                    retrieval_type="general_knowledge",
                )
                yield emit_thinking_section_start("知识库检索摘要", stage="knowledge")
                yield from yield_thinking_summary(
                    kb_bundle.summary_text,
                    stage="knowledge",
                )
                yield emit_thinking_section_end(stage="knowledge")

        # 沉淀检索摘要，供后续报告整合复用
        check_cancelled()
        try:
            session_store.save_report(
                session_id,
                topic=topic,
                markdown=session.last_report_md or "",
                search_summary=bundle.summary_text or "",
                kb_summary=kb_bundle.summary_text if kb_ids else "",
                kb_ids=kb_ids or None,
                request_id=request_id,
            )
        except Exception as persist_search:
            logger.warning(f"写入讨论检索摘要失败: {persist_search}")

        pace(CHAT_PROGRESS_DELAY_SEC)
        check_cancelled()
        yield emit_thinking_done("思考结束")

        # 刷新 session（save_report 后）
        session = session_store.get(session_id)
        citation_tips = build_citation_tips(
            search_bundle=bundle,
            kb_bundle=kb_bundle if kb_ids else None,
        )
        citation_catalog = format_citation_catalog(citation_tips)
        tip_emitter = CitationTipEmitter(citation_tips)
        user_prompt = build_chat_user_prompt(
            instruction=instruction,
            topic=topic,
            last_report_md=session.last_report_md,
            recent_messages=recent_conversation,
            search_summary=bundle.summary_text or "",
            knowledge_summary=kb_bundle.summary_text if kb_ids else "",
            citation_catalog=citation_catalog,
        )

        yield from yield_progress(
            f"正在生成回复（{llm_cfg['model_name']}）...",
            delay=CHAT_PROGRESS_DELAY_SEC,
        )

        reply_parts: list[str] = []
        last_ping = time.time()
        last_chunk_emit = 0.0

        for delta in stream_chat(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            model_name=llm_cfg["model_name"],
            system_prompt=build_chat_system_prompt(),
            user_prompt=user_prompt,
            max_tokens=CHAT_LLM_MAX_TOKENS,
            temperature=CHAT_LLM_TEMPERATURE,
            check_connectivity=False,
            cancellation=cancellation,
        ):
            check_cancelled()
            if timed_out():
                if not result_started:
                    yield emit_result_start(title=topic, file_path="")
                    result_started = True
                tail = "\n\n> （已达时间上限，回复在此截断）"
                reply_parts.append(tail)
                if cancellation:
                    cancellation.append_partial(tail)
                yield emit_result_chunk(tail)
                break

            if not result_started:
                yield emit_result_start(title=topic, file_path="")
                result_started = True

            if time.time() - last_ping > 15:
                yield emit_ping()
                last_ping = time.time()

            now = time.time()
            wait = CHAT_RESULT_CHUNK_DELAY_SEC - (now - last_chunk_emit)
            if wait > 0:
                time.sleep(wait)
            check_cancelled()

            reply_parts.append(delta)
            if cancellation:
                cancellation.append_partial(delta)
            for kind, payload in tip_emitter.feed(delta):
                if kind == "tip":
                    yield payload
                else:
                    yield emit_result_chunk(payload)
            last_chunk_emit = time.time()

        for kind, payload in tip_emitter.flush():
            if kind == "tip":
                yield payload
            elif payload:
                yield emit_result_chunk(payload)

        full_reply = remap_citation_markers_by_appearance(
            "".join(reply_parts).strip()
        )
        if not full_reply and result_started:
            yield emit_result_chunk("> 模型未返回正文，请检查 LLM 配置。")

        check_cancelled()
        if instruction:
            session_store.append_message(session_id, "user", instruction)
        if full_reply:
            session_store.append_message(session_id, "assistant", full_reply)

        merged_knowledge_refs = list(template_refs) + list(knowledge_refs)
        try:
            history_tips = tip_emitter.emitted_payloads()
            history_citations = tip_emitter.emitted_citations()
            session_store.record_ai_turn(
                session_id,
                content=full_reply or "模型未返回正文，请检查 LLM 配置。",
                metadata={
                    "module_name": "舆情事件分析",
                    "request_id": request_id,
                    "source_request_id": meta.get("source_request_id") or "",
                    "resolved_topic": resolved_topic,
                    "topic_source": topic_source,
                    "turn_status": "completed",
                    "cancelled": False,
                    "mode": 1,
                    "mode_name": "对话",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {
                        "terms_tried": bundle.terms_tried,
                        "hit_count": bundle.total_count,
                        "summary": bundle.summary_text,
                    },
                    "knowledge": {
                        "knowledge_base_ids": kb_ids,
                        "references": merged_knowledge_refs,
                        "summary": kb_bundle.summary_text if kb_ids else (
                            f"已推送 {len(template_refs)} 个报告版式模板"
                            if template_refs
                            else ""
                        ),
                    },
                    "sse": {
                        "thinking_done": True,
                        "result_done": result_started,
                        "reference_done": True,
                    },
                    "tips": history_tips,
                    "citations": history_citations,
                    "citation_count": len(history_citations),
                    "errors": [] if full_reply else ["模型未返回正文"],
                },
            )
        except Exception as persist_error:
            logger.warning(f"写入 event2 对话会话失败: {persist_error}")

        if result_started:
            yield emit_result_end(file_path="")

        yield emit_thinking_done(
            success=bool(full_reply),
            reports=[{"index": REPORT_INDEX, "file_path": ""}],
            markdown_ready=False,
            message="" if full_reply else "模型未返回正文",
            mode=1,
        )
        yield emit_reference_done()

    except GenerationCancelled:
        raise
    except Exception as e:
        if cancellation is not None and cancellation.cancelled:
            raise GenerationCancelled(cancellation.cancel_reason or "user") from e
        logger.exception(f"对话模式失败: {e}")
        try:
            session_store.record_ai_turn(
                session_id,
                content=f"回复失败：{e}",
                metadata={
                    "module_name": "舆情事件分析",
                    "request_id": meta.get("request_id") or "",
                    "source_request_id": meta.get("source_request_id") or "",
                    "resolved_topic": resolved_topic,
                    "topic_source": topic_source,
                    "turn_status": "failed",
                    "cancelled": False,
                    "mode": 1,
                    "mode_name": "对话",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {
                        "terms_tried": bundle.terms_tried,
                        "hit_count": bundle.total_count,
                        "summary": bundle.summary_text,
                    },
                    "knowledge": {
                        "knowledge_base_ids": kb_ids,
                        "references": template_refs,
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
            logger.warning(f"写入 event2 对话错误会话失败: {persist_error}")
        yield from yield_progress(f"回复失败：{e}", delay=CHAT_PROGRESS_DELAY_SEC)
        if result_started:
            yield emit_result_end(file_path="")
        yield emit_thinking_done(success=False, message=str(e), mode=1)
        yield emit_reference_done()
