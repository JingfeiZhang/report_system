"""Report Agent 编排：INGEST（P1）→ PLAN → RETRIEVE → MERGE（→ WRITE 在 pipeline）。"""

from __future__ import annotations

import os
from typing import Generator, List, Optional, Sequence, Tuple, Union

from singleAgent.report_agent.models import (
    EventBrief,
    ReportAgentResult,
    ReportTraceStep,
)
from singleAgent.report_agent.planner import plan_report_sections
from singleAgent.report_agent.prompts import build_writer_user_prompt
from singleAgent.report_agent.retrieval import format_evidence_summary, retrieve_sections
from singleAgent.report_agent.writer import write_per_section_enabled
from singleAgent.report_template_loader import ReportTemplate

ProgressEvent = Tuple[str, Union[str, ReportAgentResult]]


def is_report_agent_mode() -> bool:
    raw = (os.getenv("REPORT_AGENT_MODE") or "legacy").strip().lower()
    return raw in {"agent", "1", "true", "yes", "on"}


def iter_report_agent_phases(
    *,
    event_brief: EventBrief,
    template: ReportTemplate,
    topic: str,
    metadata: dict,
    llm_cfg: Optional[dict],
    started: float,
    retrieval_budget_sec: float,
    kb_ids: Optional[Sequence[str]] = None,
) -> Generator[ProgressEvent, None, None]:
    """yield ("progress", msg) | ("thinking", summary) | ("result", ReportAgentResult)"""
    trace: List[ReportTraceStep] = []

    yield ("progress", "正在按模板规划报告结构与查库需求...")
    sections, plan_src = plan_report_sections(
        event_brief=event_brief,
        template=template,
        topic=topic,
        llm_cfg=llm_cfg,
    )
    trace.append(
        ReportTraceStep(
            "PLAN",
            f"规划 {len(sections)} 个章节（{plan_src}）",
            {"sections": [s.title for s in sections]},
        )
    )
    yield (
        "progress",
        f"已规划 {len(sections)} 个章节：{'、'.join(s.title for s in sections[:4])}"
        + ("…" if len(sections) > 4 else ""),
    )

    progress_queue: List[str] = []

    def _on_progress(msg: str) -> None:
        progress_queue.append(msg)

    evidences, search_bundle, retr_trace, retrieval_count, kb_summary = retrieve_sections(
        sections=sections,
        event_brief=event_brief,
        llm_cfg=llm_cfg,
        started=started,
        retrieval_budget_sec=retrieval_budget_sec,
        kb_ids=kb_ids,
        on_progress=_on_progress,
    )
    trace.extend(retr_trace)
    for msg in progress_queue:
        yield ("progress", msg)

    summary_text = format_evidence_summary(sections, evidences)
    total_hits = sum(len(e.hits) for e in evidences)
    partial_n = sum(1 for e in evidences if e.status == "partial")
    yield (
        "progress",
        f"正在整理各节证据（共 {total_hits} 条"
        + (f"，{partial_n} 节部分满足" if partial_n else "")
        + "）...",
    )
    trace.append(
        ReportTraceStep(
            "MERGE",
            f"合并 {total_hits} 条节级证据；partial={partial_n}",
        )
    )

    writer_block = build_writer_user_prompt(
        topic=topic,
        event_brief=event_brief,
        template=template,
        metadata=metadata,
        section_evidences=evidences,
        sections=sections,
        knowledge_summary=kb_summary,
    )

    result = ReportAgentResult(
        sections=sections,
        evidences=evidences,
        search_bundle=search_bundle,
        evidence_summary_text=summary_text,
        writer_context_block=writer_block,
        trace=trace,
        retrieval_count=retrieval_count,
        kb_summary=kb_summary,
        write_per_section=write_per_section_enabled(),
    )
    yield ("thinking", summary_text)
    yield ("result", result)
