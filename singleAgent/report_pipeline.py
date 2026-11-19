"""event2 报告模式（mode=0）：达梦检索 + 流式 Markdown 报告。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from loguru import logger

from singleAgent.cancellation import GenerationCancelled, GenerationHandle
from singleAgent.common import pace, save_markdown, yield_progress, yield_thinking_summary
from singleAgent.config import (
    LLM_MAX_TOKENS,
    PHASE_GAP_DELAY_SEC,
    PIPELINE_SOFT_TIMEOUT_SEC,
    PRE_LLM_DELAY_SEC,
    REPORT_LLM_TEMPERATURE,
    REPORT_LLM_TIMEOUT_SEC,
    REPORT_RETRIEVAL_BUDGET_SEC,
    REPORT_INDEX,
    RESULT_CHUNK_DELAY_SEC,
    SEARCH_PLAN_ROUNDS,
    SEARCH_ROUND_DELAY_SEC,
    TARGET_HITS,
)
from singleAgent.citations import (
    CitationTipEmitter,
    build_citation_tips,
    format_citation_catalog,
    remap_citation_markers_by_appearance,
)
from singleAgent.context import build_fast_context
from singleAgent.dm_search import SearchBundle
from singleAgent.event2_retrieval import (
    is_event2_agent_retrieval,
    is_event2_mcp_retrieval,
    retrieve_event2_context,
)
from singleAgent.knowledge_base import KnowledgeBundle, retrieve as retrieve_knowledge
from singleAgent.llm import resolve_llm_settings, stream_chat
from singleAgent.prompts import build_system_prompt, build_user_prompt
from singleAgent.report_template_loader import (
    load_report_template,
    resolve_template_id,
)
from singleAgent.report_agent.event_ingest import ingest_event_brief
from singleAgent.report_agent.orchestrator import is_report_agent_mode, iter_report_agent_phases
from singleAgent.report_agent.prompts import build_report_writer_system
from singleAgent.report_agent.writer import polish_enabled, polish_max_tokens
from singleAgent.search_planner import (
    build_initial_search_plan,
    normalize_topic,
    refine_terms_via_llm,
)
from singleAgent.session_store import SessionStore, build_recent_conversation_context
from singleAgent.result_stream import ResultStreamClassifier, StreamPiece, extract_body_markdown
from singleAgent.sse import (
    emit_ping,
    emit_reference_done,
    emit_result_end,
    emit_result_start,
    emit_result_chunk,
    emit_result_typed,
    emit_retrieval_knowledge,
    emit_thinking_done,
    emit_thinking_section_end,
    emit_thinking_section_start,
)


def run_report_pipeline(
    event: Any,
    config: Dict[str, Any],
    *,
    llm_override: Optional[Dict[str, str]] = None,
    project_root: Optional[Path] = None,
    session_store: SessionStore,
    session_id: str,
    started: float,
    cancellation: Optional[GenerationHandle] = None,
) -> Generator[str, None, None]:
    root = project_root or Path(__file__).resolve().parent.parent
    topic = event.topic or event.query or "分析报告"
    meta = event.metadata or {}
    md_rel_path = ""
    result_started = False
    bundle: SearchBundle = SearchBundle()
    kb_bundle = KnowledgeBundle()
    kb_ids: List[str] = []
    request_id = str(meta.get("request_id") or "").strip()
    resolved_topic = ""
    topic_source = ""
    agent_result = None

    def timed_out() -> bool:
        return (time.time() - started) >= PIPELINE_SOFT_TIMEOUT_SEC

    def retrieval_budget_exhausted() -> bool:
        return (time.time() - started) >= REPORT_RETRIEVAL_BUDGET_SEC

    def check_cancelled() -> None:
        if cancellation:
            cancellation.raise_if_cancelled()

    try:
        check_cancelled()
        session = session_store.get(session_id)
        instruction = (meta.get("instruction") or event.query or topic or "").strip()
        session_topic = normalize_topic(session.last_topic)

        # 空 instruction + 已选模板：按会话主题补默认生成意图
        from singleAgent.ext_utils import (
            default_instruction_for_template_report,
            ext_has_template_selection,
        )

        ext_payload = meta.get("ext") if isinstance(meta.get("ext"), dict) else {}
        if not instruction and ext_has_template_selection(ext_payload):
            instruction = default_instruction_for_template_report(session_topic)
            meta = dict(meta)
            meta["instruction"] = instruction
            if session_topic and (
                not (topic or "").strip() or topic.strip() in ("未命名主题", "分析报告")
            ):
                topic = session_topic

        recent_conversation = build_recent_conversation_context(
            session,
            exclude_request_id=request_id,
        )
        meta = dict(meta)
        meta["conversation_context"] = recent_conversation
        event.metadata = meta

        llm_cfg = resolve_llm_settings(config, llm_override)

        yield from yield_progress("正在解析事件、案例与附件...")
        yield from yield_progress("正在认定分析主题...")

        check_cancelled()
        plan = build_initial_search_plan(
            instruction=instruction,
            session_topic=session_topic,
            llm_cfg=llm_cfg,
            conversation_context=recent_conversation,
            strict_context=True,
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
            f"event2 report topic resolved session={session_id} request={request_id} "
            f"topic={resolved_topic} source={topic_source}"
        )
        anchor_topic = topic

        yield from yield_progress(
            f"认定主题：{topic}"
            + ("" if plan.topic_source == "llm" else f"（{plan.topic_source}）")
        )

        yield from yield_progress("正在阅读事件概述（附件）...")
        check_cancelled()
        event_brief = ingest_event_brief(
            metadata=meta,
            topic=topic,
            llm_cfg=llm_cfg,
        )
        meta["event_brief"] = event_brief.to_dict()
        event.metadata = meta
        # mode0 报告态始终使用主文档 / 库内辅证语义（无附件时为降级 EventBrief）
        use_primary_doc = True

        ctx = build_fast_context(
            topic=topic,
            query=event.query or topic,
            metadata=meta,
            anchor_topic=anchor_topic,
            event_brief=event_brief,
            primary_event_doc=use_primary_doc,
        )

        # mode0：按 ext 所选模板生成。不再回推 retrieval/knowledge，
        # 避免第二次搜索时把「已选模板」再刷到右栏（第一轮已列出全部模板）。
        template = load_report_template(
            resolve_template_id(meta),
            project_root=root,
            session_id=session_id,
        )
        check_cancelled()

        terms = list(plan.terms)
        use_report_agent = is_report_agent_mode() and is_event2_mcp_retrieval()

        from singleAgent.knowledge_base import normalize_knowledge_base_ids

        kb_ids = meta.get("knowledge_base_ids") or []
        if isinstance(kb_ids, str):
            try:
                kb_ids = json.loads(kb_ids)
            except (TypeError, ValueError):
                kb_ids = []
        if not isinstance(kb_ids, list):
            kb_ids = []
        kb_ids = normalize_knowledge_base_ids(kb_ids)

        if use_report_agent:
            check_cancelled()
            for event_kind, payload in iter_report_agent_phases(
                event_brief=event_brief,
                template=template,
                topic=topic,
                metadata=meta,
                llm_cfg=llm_cfg,
                started=started,
                retrieval_budget_sec=REPORT_RETRIEVAL_BUDGET_SEC,
                kb_ids=kb_ids,
            ):
                check_cancelled()
                if event_kind == "progress":
                    yield from yield_progress(str(payload))
                elif event_kind == "thinking":
                    if not payload:
                        continue
                    pace(PHASE_GAP_DELAY_SEC)
                    yield emit_thinking_section_start("检索摘要", stage="search")
                    yield from yield_thinking_summary(str(payload), stage="search")
                    yield emit_thinking_section_end(stage="search")
                elif event_kind == "result":
                    agent_result = payload
            if agent_result is not None:
                bundle = agent_result.search_bundle
                meta["report_agent"] = {
                    "mode": "agent",
                    "sections": [s.title for s in agent_result.sections],
                    "retrieval_count": agent_result.retrieval_count,
                    "write_per_section": agent_result.write_per_section,
                    "section_status": [
                        {"id": e.section_id, "title": e.section_title, "status": e.status}
                        for e in agent_result.evidences
                    ],
                    "trace": [
                        {"stage": t.stage, "message": t.message}
                        for t in agent_result.trace
                    ],
                }
                event.metadata = meta
                if bundle.empty:
                    yield from yield_progress(
                        agent_result.evidence_summary_text
                        or "达梦库未返回可用上下文，将依据事件概述继续成稿"
                    )
                else:
                    yield from yield_progress(
                        f"Report Agent 已完成 {agent_result.retrieval_count} 轮查库，"
                        f"共 {bundle.total_count} 条节级辅证"
                    )
            else:
                bundle = SearchBundle(
                    empty=True,
                    summary_text="Report Agent 未返回检索结果。",
                )
        elif is_event2_mcp_retrieval():
            check_cancelled()
            if is_event2_agent_retrieval():
                yield from yield_progress(
                    "TROOPS Agent：正在根据表注释选表并查询达梦库..."
                )
            else:
                yield from yield_progress("正在通过 MCP 读取达梦业务库...")
            check_cancelled()
            bundle = retrieve_event2_context(
                topic=topic,
                search_text=instruction,
                llm_cfg=llm_cfg,
                event_brief=event_brief,
                cancel_check=check_cancelled,
            )
            check_cancelled()
            if bundle.empty:
                yield from yield_progress(
                    bundle.summary_text or "达梦库未返回可用上下文，将依据案例与附件继续分析"
                )
            else:
                label = (
                    "Agent 已加载"
                    if is_event2_agent_retrieval()
                    else "达梦库已加载"
                )
                yield from yield_progress(
                    f"{label} {bundle.total_count} 条上下文片段"
                )
        elif terms:
            yield from yield_progress(
                f"模型拟定检索词（第1轮）：{'、'.join(terms)}"
                + ("" if plan.terms_source == "llm" else f"（{plan.terms_source}）")
            )
            from singleAgent.dm_search import (
                _open_search_db,
                finalize_search_bundle,
                search_one_term,
            )

            merged: List[Any] = []
            seen_keys: Set[Tuple[str, str, str]] = set()
            tried: List[str] = []

            db = _open_search_db()
            if db is None:
                bundle = SearchBundle(
                    terms_tried=terms,
                    empty=True,
                    summary_text="数据库检索模块不可用。",
                )
            else:
                plan_rounds = max(1, SEARCH_PLAN_ROUNDS)
                for round_idx in range(plan_rounds):
                    check_cancelled()
                    if timed_out() or retrieval_budget_exhausted():
                        break
                    round_terms = terms if round_idx == 0 else []
                    if round_idx > 0:
                        if len(merged) >= TARGET_HITS:
                            break
                        interim = finalize_search_bundle(tried, merged)
                        yield from yield_progress(
                            f"根据第{round_idx}轮命中，正在规划补充检索词..."
                        )
                        more, refine_src = refine_terms_via_llm(
                            topic=topic,
                            used_terms=tried,
                            hit_summary=interim.summary_text,
                            llm_cfg=llm_cfg,
                            check_connectivity=False,
                        )
                        check_cancelled()
                        if not more:
                            yield from yield_progress("无需补充检索词，结束检索")
                            break
                        round_terms = more
                        yield from yield_progress(
                            f"模型拟定检索词（第{round_idx + 1}轮）：{'、'.join(round_terms)}"
                            + ("" if refine_src == "llm" else f"（{refine_src}）")
                        )

                    for term in round_terms:
                        check_cancelled()
                        if (
                            timed_out()
                            or retrieval_budget_exhausted()
                            or len(merged) >= TARGET_HITS
                        ):
                            break
                        if term in tried:
                            continue
                        pace(SEARCH_ROUND_DELAY_SEC)
                        check_cancelled()
                        tried.append(term)
                        search_one_term(db, term, merged, seen_keys)
                        check_cancelled()
                        yield from yield_progress(f"已完成检索：「{term}」")

                    if len(merged) >= TARGET_HITS:
                        break

                bundle = finalize_search_bundle(tried, merged)
        else:
            from singleAgent.dm_search import finalize_search_bundle

            yield from yield_progress("未得到检索词，将主要依据案例与附件分析")
            bundle = finalize_search_bundle([], [])

        # kb_ids 已在检索前解析

        if bundle.empty and not is_event2_mcp_retrieval():
            yield from yield_progress("库内暂无匹配数据，将依据案例与附件继续分析")
        elif not bundle.empty and not use_report_agent:
            yield from yield_progress(
                f"库内共命中 {bundle.total_count} 条，正在整理检索摘要..."
                if not is_event2_mcp_retrieval()
                else f"正在整理达梦库上下文摘要（{bundle.total_count} 条）...",
            )
            pace(PHASE_GAP_DELAY_SEC)
            yield emit_thinking_section_start("检索摘要", stage="search")
            yield from yield_thinking_summary(bundle.summary_text, stage="search")
            yield emit_thinking_section_end(stage="search")

        if kb_ids:
            check_cancelled()
            yield from yield_progress(f"计划检索知识库：{'、'.join(str(i) for i in kb_ids)}")
            if timed_out() or retrieval_budget_exhausted():
                kb_bundle = KnowledgeBundle(
                    kb_ids=kb_ids,
                    summary_text="已达时间上限，跳过知识库检索。",
                )
            else:
                pace(PHASE_GAP_DELAY_SEC)
                check_cancelled()
                kb_bundle = retrieve_knowledge(anchor_topic or ctx.search_text, kb_ids)
                check_cancelled()
                if kb_bundle.empty:
                    # Report Agent 可能已按 need 查过知识库
                    if agent_result is not None and agent_result.kb_summary:
                        kb_bundle = KnowledgeBundle(
                            kb_ids=kb_ids,
                            summary_text=agent_result.kb_summary,
                            empty=False,
                        )
                        yield from yield_progress("已复用 Report Agent 节级知识库辅证")
                    else:
                        yield from yield_progress("知识库暂无匹配片段，将依据舆情库与案例继续分析")
                else:
                    yield from yield_progress(
                        f"知识库共命中 {kb_bundle.total_count} 条，正在整理检索摘要...",
                    )
                    pace(PHASE_GAP_DELAY_SEC)
                    yield emit_retrieval_knowledge(
                        refs=kb_bundle.to_knowledge_refs(),
                        message=f"检索完成: {kb_bundle.total_count} 条",
                        session_id=session_id,
                        progress=kb_bundle.total_count,
                        retrieval_type="general_knowledge",
                    )
                    yield emit_thinking_section_start("知识库检索摘要", stage="knowledge")
                    yield from yield_thinking_summary(
                        kb_bundle.summary_text,
                        stage="knowledge",
                    )
                    yield emit_thinking_section_end(stage="knowledge")

        search_summary_for_ctx = (
            agent_result.evidence_summary_text
            if agent_result is not None
            else bundle.summary_text
        )
        ctx = build_fast_context(
            topic=topic,
            query=event.query or topic,
            metadata=meta,
            search_summary=search_summary_for_ctx,
            knowledge_summary=kb_bundle.summary_text if kb_ids else "",
            anchor_topic=anchor_topic,
            event_brief=event_brief,
            primary_event_doc=use_primary_doc,
        )

        citation_tips = build_citation_tips(
            search_bundle=bundle,
            kb_bundle=kb_bundle if kb_ids else None,
        )
        citation_catalog = format_citation_catalog(citation_tips)
        tip_emitter = CitationTipEmitter(citation_tips)

        pace(PHASE_GAP_DELAY_SEC)
        check_cancelled()
        yield emit_thinking_done("思考结束")

        if agent_result is not None:
            system_prompt = build_report_writer_system(
                section_mode=bool(agent_result.write_per_section)
            )
            prompt_body = agent_result.writer_context_block
            if citation_catalog and not agent_result.write_per_section:
                prompt_body = f"{prompt_body}\n\n{citation_catalog}"
            user_prompt = build_user_prompt(prompt_body)
        else:
            system_prompt = build_system_prompt(
                topic=topic,
                metadata=meta,
                project_root=root,
            )
            prompt_body = ctx.user_prompt
            if citation_catalog:
                prompt_body = f"{prompt_body}\n\n{citation_catalog}"
            user_prompt = build_user_prompt(prompt_body)
        generation_max_tokens = min(template.max_output_tokens, LLM_MAX_TOKENS)

        yield from yield_progress(
            f"正在连接模型并生成分析（{llm_cfg['model_name']} @ {llm_cfg['base_url']}）...",
        )
        pace(PRE_LLM_DELAY_SEC)
        check_cancelled()

        markdown_parts: list[str] = []
        body_parts: list[str] = []
        classifier = ResultStreamClassifier()
        last_ping = time.time()
        last_chunk_emit = 0.0
        # 正文始终边生成边 SSE。润色若整篇 defer 再吐，前端会「一股脑」出字；
        # 故润色只改落盘/历史，不二次推送前端。
        defer_result = False
        do_polish = polish_enabled()

        def _emit_pieces(
            pieces: list[StreamPiece],
            *,
            to_client: bool = True,
        ) -> Generator[str, None, None]:
            nonlocal result_started, last_ping, last_chunk_emit
            for piece in pieces:
                check_cancelled()
                if not piece.text:
                    continue
                markdown_parts.append(piece.text)
                if piece.stream_type == "chunk":
                    body_parts.append(piece.text)
                if not to_client:
                    continue
                if cancellation:
                    cancellation.append_partial(piece.text)
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

                if piece.stream_type == "chunk":
                    for kind, payload in tip_emitter.feed(piece.text):
                        if kind == "tip":
                            yield payload
                        else:
                            yield emit_result_typed(payload, "chunk", None)
                else:
                    yield emit_result_typed(piece.text, piece.stream_type, piece.aux_kind)
                last_chunk_emit = time.time()

        def _stream_one_prompt(
            *,
            sys_prompt: str,
            usr_prompt: str,
            max_tokens: int,
            to_client: Optional[bool] = None,
        ) -> Generator[str, None, None]:
            stream_out = True if to_client is None else to_client
            local_classifier = ResultStreamClassifier()
            for delta in stream_chat(
                api_key=llm_cfg["api_key"],
                base_url=llm_cfg["base_url"],
                model_name=llm_cfg["model_name"],
                system_prompt=sys_prompt,
                user_prompt=usr_prompt,
                max_tokens=max_tokens,
                temperature=REPORT_LLM_TEMPERATURE,
                timeout=REPORT_LLM_TIMEOUT_SEC,
                check_connectivity=False,
                cancellation=cancellation,
            ):
                check_cancelled()
                if timed_out():
                    if stream_out and not result_started:
                        yield emit_result_start(title=topic, file_path="")
                        result_started = True
                    tail = "\n\n> （已达快模式时间上限，输出在此截断）"
                    markdown_parts.append(tail)
                    body_parts.append(tail)
                    if cancellation:
                        cancellation.append_partial(tail)
                    if stream_out:
                        yield emit_result_chunk(tail)
                    break
                if not delta:
                    continue
                yield from _emit_pieces(
                    local_classifier.feed(delta),
                    to_client=stream_out,
                )
            yield from _emit_pieces(
                local_classifier.flush(),
                to_client=stream_out,
            )

        if (
            agent_result is not None
            and agent_result.write_per_section
            and agent_result.sections
        ):
            from singleAgent.report_agent.prompts import (
                build_report_writer_system as _bws,
                build_section_writer_user_prompt,
            )
            from singleAgent.report_agent.writer import (
                catalog_for_section,
                pair_sections_evidences,
            )

            section_sys = _bws(section_mode=True)
            paired = pair_sections_evidences(
                agent_result.sections,
                agent_result.evidences,
            )
            per_tokens = max(
                400,
                min(
                    generation_max_tokens,
                    max(600, generation_max_tokens // max(1, len(paired)) + 200),
                ),
            )
            for idx, (sec, ev) in enumerate(paired):
                check_cancelled()
                if timed_out():
                    break
                yield from yield_progress(f"正在撰写：{sec.title}…")
                sec_catalog = catalog_for_section(
                    citation_tips,
                    evidence=ev,
                    global_tips=citation_tips,
                ) or citation_catalog
                sec_user = build_user_prompt(
                    build_section_writer_user_prompt(
                        topic=topic,
                        event_brief=event_brief,
                        template=template,
                        metadata=meta,
                        section=sec,
                        evidence=ev,
                        all_sections=agent_result.sections,
                        citation_catalog=sec_catalog,
                        knowledge_summary=ev.kb_summary
                        or (agent_result.kb_summary if idx == 0 else ""),
                    )
                )
                yield from _stream_one_prompt(
                    sys_prompt=section_sys,
                    usr_prompt=sec_user,
                    max_tokens=per_tokens,
                )
                # 节间分隔
                if idx < len(paired) - 1 and not timed_out():
                    sep = "\n\n"
                    markdown_parts.append(sep)
                    body_parts.append(sep)
                    if result_started and not defer_result:
                        yield emit_result_chunk(sep)
        else:
            yield from _stream_one_prompt(
                sys_prompt=system_prompt,
                usr_prompt=user_prompt,
                max_tokens=generation_max_tokens,
            )

        # 润色：仅提升落盘/历史正文质量，不再二次 SSE（避免挡住或整篇重吐）
        draft_md = "".join(markdown_parts).strip()
        if do_polish and draft_md and not timed_out():
            from singleAgent.report_agent.prompts import (
                build_polish_system,
                build_polish_user_prompt,
            )

            yield from yield_progress("正在统一版式与润色（仅落盘）…")
            polish_parts: list[str] = []
            try:
                for delta in stream_chat(
                    api_key=llm_cfg["api_key"],
                    base_url=llm_cfg["base_url"],
                    model_name=llm_cfg["model_name"],
                    system_prompt=build_polish_system(),
                    user_prompt=build_polish_user_prompt(
                        draft_markdown=draft_md,
                        template_name=getattr(template, "name", "") or "",
                        event_brief=event_brief,
                    ),
                    max_tokens=min(
                        generation_max_tokens,
                        polish_max_tokens(generation_max_tokens),
                    ),
                    temperature=min(REPORT_LLM_TEMPERATURE, 0.35),
                    timeout=REPORT_LLM_TIMEOUT_SEC,
                    check_connectivity=False,
                    cancellation=cancellation,
                ):
                    check_cancelled()
                    if timed_out():
                        break
                    if delta:
                        polish_parts.append(delta)
                polished = "".join(polish_parts).strip()
                if polished and len(polished) >= max(80, int(len(draft_md) * 0.35)):
                    markdown_parts.clear()
                    markdown_parts.append(polished)
                    body_parts.clear()
                    body_parts.append(extract_body_markdown(polished) or polished)
                else:
                    logger.warning("报告润色结果过短，落盘仍用流式草稿")
            except Exception as polish_exc:
                logger.warning(f"报告润色失败，落盘仍用流式草稿: {polish_exc}")
        for kind, payload in tip_emitter.flush():
            if kind == "tip":
                yield payload
            elif payload:
                # hold 区原文已计入 markdown_parts，仅补发 SSE
                yield emit_result_chunk(payload)

        full_md = remap_citation_markers_by_appearance(
            "".join(markdown_parts).strip()
        )
        body_md = remap_citation_markers_by_appearance(
            "".join(body_parts).strip()
        ) or extract_body_markdown(full_md)
        if not full_md and result_started:
            yield emit_result_chunk("> 模型未返回正文，请检查 LLM 配置与连通性。")

        if full_md:
            check_cancelled()
            md_rel_path = save_markdown(topic, full_md, root)
            check_cancelled()
            session_store.save_report(
                session_id,
                topic,
                body_md,
                search_summary=bundle.summary_text,
                kb_summary=kb_bundle.summary_text if kb_ids else "",
                kb_ids=kb_ids,
                request_id=request_id,
            )
            instruction = (meta.get("instruction") or "").strip()
            if instruction:
                session_store.append_message(session_id, "user", instruction)
            session_store.append_message(session_id, "assistant", body_md)

        check_cancelled()
        try:
            history_tips = tip_emitter.emitted_payloads()
            history_citations = tip_emitter.emitted_citations()
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
                    "request_id": request_id,
                    "source_request_id": meta.get("source_request_id") or "",
                    "resolved_topic": resolved_topic,
                    "topic_source": topic_source,
                    "turn_status": "completed",
                    "cancelled": False,
                    "mode": 0,
                    "mode_name": "新分析报告",
                    "markdown_ready": bool(body_md),
                    "reports": [
                        {
                            "index": REPORT_INDEX,
                            "file_path": md_rel_path,
                            "report_text": full_md,
                        }
                    ] if full_md else [],
                    "search": {
                        "terms_tried": bundle.terms_tried,
                        "hit_count": bundle.total_count,
                        "summary": bundle.summary_text,
                    },
                    "knowledge": {
                        "knowledge_base_ids": kb_ids,
                        "references": kb_bundle.to_knowledge_refs(),
                        "summary": kb_bundle.summary_text,
                    },
                    "sse": {
                        "thinking_done": True,
                        "result_done": result_started,
                        "reference_done": True,
                    },
                    "tips": history_tips,
                    "citations": history_citations,
                    "citation_count": len(history_citations),
                    "errors": [] if body_md else ["模型未返回正文"],
                },
            )
        except Exception as persist_error:
            logger.warning(f"写入 event2 报告会话失败: {persist_error}")

        if result_started:
            yield emit_result_end(file_path=md_rel_path)

        yield emit_thinking_done(
            success=bool(body_md),
            reports=[{"index": REPORT_INDEX, "file_path": md_rel_path}],
            markdown_ready=bool(body_md),
            message="" if body_md else "模型未返回正文",
            mode=0,
        )
        yield emit_reference_done()

    except GenerationCancelled:
        raise
    except Exception as e:
        if cancellation is not None and cancellation.cancelled:
            raise GenerationCancelled(cancellation.cancel_reason or "user") from e
        logger.exception(f"报告模式失败: {e}")
        try:
            session_store.record_ai_turn(
                session_id,
                content=f"舆情事件分析失败：{e}",
                metadata={
                    "module_name": "舆情事件分析",
                    "request_id": meta.get("request_id") or "",
                    "source_request_id": meta.get("source_request_id") or "",
                    "resolved_topic": resolved_topic,
                    "topic_source": topic_source,
                    "turn_status": "failed",
                    "cancelled": False,
                    "mode": 0,
                    "mode_name": "新分析报告",
                    "markdown_ready": False,
                    "reports": [],
                    "search": {
                        "terms_tried": bundle.terms_tried,
                        "hit_count": bundle.total_count,
                        "summary": bundle.summary_text,
                    },
                    "knowledge": {
                        "knowledge_base_ids": kb_ids,
                        "references": kb_bundle.to_knowledge_refs(),
                        "summary": kb_bundle.summary_text,
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
            logger.warning(f"写入 event2 报告错误会话失败: {persist_error}")
        yield from yield_progress(f"分析失败：{e}")
        if result_started:
            yield emit_result_end(file_path="")
        yield emit_thinking_done(success=False, message=str(e), mode=0)
        yield emit_reference_done()
