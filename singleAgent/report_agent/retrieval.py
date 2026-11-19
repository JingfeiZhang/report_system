"""Report Agent 检索循环：按节 InformationNeed 调用 TROOPS / 知识库。"""

from __future__ import annotations

import os
import time
from typing import Callable, List, Optional, Sequence, Tuple

from loguru import logger

from singleAgent.dm_mcp_search import McpSearchHit
from singleAgent.dm_search import SearchBundle, finalize_search_bundle
from singleAgent.report_agent.models import (
    EventBrief,
    InformationNeed,
    ReportTraceStep,
    SectionEvidence,
    SectionPlan,
)
from singleAgent.report_agent.table_resolve import (
    load_business_catalog_safe,
    resolve_tables_from_hints,
    temporary_table_allowlist,
)


def _agent_int(name: str, default: int) -> int:
    return max(0, int(os.getenv(name, str(default))))


def build_need_topic(need: InformationNeed, event_brief: EventBrief) -> str:
    parts: List[str] = []
    if event_brief.title:
        parts.append(event_brief.title)
    if need.description:
        parts.append(need.description)
    parts.extend(need.keywords)
    if event_brief.entities:
        parts.extend(event_brief.entities[:8])
    if event_brief.locations:
        parts.extend(event_brief.locations[:5])
    topic = " ".join(p.strip() for p in parts if p and p.strip())
    return topic[:2000] or event_brief.summary[:500] or "事件辅证检索"


def broaden_need_topic(
    need: InformationNeed,
    event_brief: EventBrief,
    *,
    attempt: int,
) -> str:
    """失败重试时放宽关键词（去掉过细约束）。"""
    base = build_need_topic(need, event_brief)
    if attempt <= 0:
        return base
    loose_parts = [
        event_brief.title,
        " ".join(event_brief.entities[:5]),
        " ".join(event_brief.locations[:3]),
        " ".join(event_brief.event_types[:3]),
        " ".join(need.keywords[:3]),
        "部队 部署 日志",
    ]
    loose = " ".join(p for p in loose_parts if p and str(p).strip())
    return (loose or base)[:2000]


def _format_need_keywords(need: InformationNeed, *, attempt: int = 0) -> str:
    """思维链展示用：本轮检索关键词。"""
    kws = [str(k).strip() for k in (need.keywords or []) if str(k).strip()]
    if attempt > 0:
        # 重试时提示已放宽，仍展示规划关键词 + 放宽标记
        if kws:
            return "、".join(kws[:8]) + "（已放宽）"
        return "（放宽主题重试）"
    if kws:
        return "、".join(kws[:8])
    # 无显式 keywords 时从 description 截一段作展示
    desc = (need.description or "").strip()
    if desc:
        return desc[:40] + ("…" if len(desc) > 40 else "")
    return "（未指定）"


def _hit_key(hit: McpSearchHit) -> Tuple[str, str]:
    return (
        str(hit.platform or ""),
        (hit.title_or_content or "")[:120],
    )


def _merge_hits(
    merged: List[McpSearchHit],
    seen: set[Tuple[str, str]],
    new_hits: List[McpSearchHit],
) -> None:
    for hit in new_hits:
        key = _hit_key(hit)
        if key in seen:
            continue
        seen.add(key)
        merged.append(hit)


def _need_wants_troops(need: InformationNeed) -> bool:
    return need.source in {"troops", "both", ""}


def _need_wants_knowledge(need: InformationNeed) -> bool:
    return need.source in {"knowledge", "both"}


def _run_troops_once(
    *,
    topic: str,
    description: str,
    event_brief: EventBrief,
    llm_cfg: Optional[dict],
    allow_tables: Sequence[str],
) -> SearchBundle:
    from singleAgent.event2_retrieval import retrieve_event2_context

    with temporary_table_allowlist(allow_tables):
        return retrieve_event2_context(
            topic=topic,
            search_text=description,
            llm_cfg=llm_cfg,
            event_brief=event_brief,
        )


def _run_knowledge_once(
    *,
    topic: str,
    kb_ids: Sequence[str],
) -> str:
    if not kb_ids:
        return ""
    try:
        from singleAgent.knowledge_base import retrieve as retrieve_knowledge

        bundle = retrieve_knowledge(topic, list(kb_ids))
        if bundle.empty:
            return ""
        return (bundle.summary_text or "").strip()
    except Exception as exc:
        logger.warning("Report knowledge retrieve failed: {}", exc)
        return ""


def retrieve_sections(
    *,
    sections: List[SectionPlan],
    event_brief: EventBrief,
    llm_cfg: Optional[dict],
    started: float,
    retrieval_budget_sec: float,
    kb_ids: Optional[Sequence[str]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[List[SectionEvidence], SearchBundle, List[ReportTraceStep], int, str]:
    max_retrievals = max(1, _agent_int("REPORT_AGENT_MAX_RETRIEVALS", 6))
    max_needs_per_section = max(1, _agent_int("REPORT_AGENT_MAX_NEEDS_PER_SECTION", 2))
    retries_per_need = _agent_int("REPORT_AGENT_RETRIES_PER_NEED", 1)
    kb_ids = [str(x).strip() for x in (kb_ids or []) if str(x).strip()]

    catalog = load_business_catalog_safe()
    trace: List[ReportTraceStep] = []
    evidences: List[SectionEvidence] = []
    all_hits: List[McpSearchHit] = []
    seen_keys: set[Tuple[str, str]] = set()
    tried_labels: List[str] = []
    retrieval_count = 0
    kb_summaries: List[str] = []

    def budget_left() -> float:
        return retrieval_budget_sec - (time.time() - started)

    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    for section in sections:
        if retrieval_count >= max_retrievals or budget_left() <= 0:
            trace.append(
                ReportTraceStep(
                    "RETRIEVE",
                    f"预算用尽，跳过剩余章节（已查 {retrieval_count} 次）",
                )
            )
            section.status = "skipped"
            evidences.append(
                SectionEvidence(
                    section_id=section.section_id,
                    section_title=section.title,
                    gaps=[f"未执行库内检索：{section.title}（预算用尽）"],
                    status="skipped",
                )
            )
            continue

        sec_hits: List[McpSearchHit] = []
        sec_seen: set[Tuple[str, str]] = set()
        queries: List[str] = []
        satisfied: List[str] = []
        gaps: List[str] = []
        sec_kb: List[str] = []
        retries_used = 0
        needs = section.needs[:max_needs_per_section] or []
        section.status = "retrieving"

        if not needs:
            gaps.append("本节无规划的信息需求")
            section.status = "skipped"
            evidences.append(
                SectionEvidence(
                    section_id=section.section_id,
                    section_title=section.title,
                    gaps=gaps,
                    status="skipped",
                )
            )
            continue

        for need in needs:
            if retrieval_count >= max_retrievals or budget_left() <= 0:
                gaps.append(f"预算用尽，未查：{need.description}")
                break

            allow_tables = resolve_tables_from_hints(
                need.suggested_tables_hint,
                catalog,
                max_tables=5,
            )
            got_hits = False

            if _need_wants_troops(need):
                for attempt in range(retries_per_need + 1):
                    if retrieval_count >= max_retrievals or budget_left() <= 0:
                        break
                    topic = broaden_need_topic(need, event_brief, attempt=attempt)
                    kw_text = _format_need_keywords(need, attempt=attempt)
                    label = (
                        f"正在查询：{section.title} — {need.description}"
                        f"｜关键词：{kw_text}"
                        + (f"（重试{attempt}）" if attempt else "")
                    )
                    progress(label)
                    trace.append(
                        ReportTraceStep(
                            "RETRIEVE",
                            f"{section.title} / {need.id}: {need.description}"
                            + (f" retry={attempt}" if attempt else ""),
                            {
                                "topic": topic[:200],
                                "keywords": list(need.keywords or [])[:8],
                                "keywords_display": kw_text,
                                "allow_tables": allow_tables,
                                "attempt": attempt,
                            },
                        )
                    )
                    try:
                        bundle = _run_troops_once(
                            topic=topic,
                            description=need.description,
                            event_brief=event_brief,
                            llm_cfg=llm_cfg,
                            allow_tables=allow_tables,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Report retrieval failed need={} attempt={}: {}",
                            need.id,
                            attempt,
                            exc,
                        )
                        retrieval_count += 1
                        retries_used += 1 if attempt else 0
                        if attempt >= retries_per_need:
                            gaps.append(f"{need.description}（检索异常）")
                        continue

                    retrieval_count += 1
                    if attempt:
                        retries_used += 1
                    tried_labels.append(f"[{section.section_id}/{need.id}/a{attempt}]")

                    if bundle.hits:
                        _merge_hits(sec_hits, sec_seen, list(bundle.hits))
                        _merge_hits(all_hits, seen_keys, list(bundle.hits))
                        satisfied.append(need.id)
                        queries.append(
                            bundle.summary_text[:300]
                            if bundle.summary_text
                            else topic[:120]
                        )
                        got_hits = True
                        break

                    queries.append(
                        bundle.summary_text[:200]
                        if bundle.summary_text
                        else f"{need.description}：库内未命中"
                    )

                if not got_hits and need.priority == "required":
                    gaps.append(need.description)

            if _need_wants_knowledge(need) and kb_ids and budget_left() > 0:
                kw_text = _format_need_keywords(need)
                progress(
                    f"正在检索知识库：{section.title} — {need.description}"
                    f"｜关键词：{kw_text}"
                )
                kb_topic = build_need_topic(need, event_brief)
                kb_text = _run_knowledge_once(topic=kb_topic, kb_ids=kb_ids)
                if kb_text:
                    sec_kb.append(kb_text)
                    kb_summaries.append(f"【{section.title}】\n{kb_text}")
                    if need.id not in satisfied:
                        satisfied.append(need.id)
                    queries.append(f"知识库：已命中片段")
                    trace.append(
                        ReportTraceStep(
                            "RETRIEVE_KB",
                            f"{section.title} / {need.id}: 知识库命中",
                        )
                    )
                else:
                    gaps.append(f"{need.description}（知识库未命中）")
                    trace.append(
                        ReportTraceStep(
                            "RETRIEVE_KB",
                            f"{section.title} / {need.id}: 知识库未命中",
                        )
                    )

        if sec_hits and not gaps:
            status = "satisfied"
        elif sec_hits or sec_kb:
            status = "partial"
        elif any("预算" in g for g in gaps):
            status = "skipped"
        else:
            status = "empty"

        section.status = status
        evidences.append(
            SectionEvidence(
                section_id=section.section_id,
                section_title=section.title,
                hits=list(sec_hits),
                queries_executed=queries,
                satisfied_needs=satisfied,
                gaps=gaps,
                kb_summary="\n".join(sec_kb),
                status=status,
                retries_used=retries_used,
            )
        )

    search_bundle = finalize_search_bundle(tried_labels or ["[Report Agent]"], all_hits)
    if not all_hits and not search_bundle.summary_text:
        search_bundle.summary_text = "Report Agent 多轮检索未返回库内记录。"
    kb_summary = "\n\n".join(kb_summaries)
    return evidences, search_bundle, trace, retrieval_count, kb_summary


def format_evidence_summary(
    sections: List[SectionPlan],
    evidences: List[SectionEvidence],
) -> str:
    lines = ["数据源：Report Agent 按模板章节多轮查询 TROOPS 达梦库（非 mock）"]
    ev_map = {e.section_id: e for e in evidences}
    total = sum(len(e.hits) for e in evidences)
    lines.append(f"共 {total} 条节级辅证片段：")
    for sec in sections:
        ev = ev_map.get(sec.section_id)
        if not ev:
            lines.append(f"- {sec.title}：未检索")
            continue
        status_label = {
            "satisfied": "已满足",
            "partial": "部分满足",
            "skipped": "已跳过",
            "empty": "无命中",
        }.get(ev.status, ev.status)
        if ev.hits:
            lines.append(f"- {sec.title}：{len(ev.hits)} 条（{status_label}）")
            for idx, hit in enumerate(ev.hits[:3], 1):
                text = (hit.title_or_content or "").replace("\n", " ")[:100]
                lines.append(f"  {idx}. [{hit.platform}] {text}")
        else:
            gap = "；".join(ev.gaps[:2]) if ev.gaps else "库内暂无支撑"
            lines.append(f"- {sec.title}：0 条（{status_label}；{gap}）")
        if ev.kb_summary:
            lines.append(f"  知识库辅证：已加载（{len(ev.kb_summary)} 字）")
        if ev.status == "partial" and ev.gaps:
            lines.append(f"  本节缺口：{'；'.join(ev.gaps[:3])}")
    return "\n".join(lines)
