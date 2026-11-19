"""Report Agent 数据结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from singleAgent.dm_mcp_search import McpSearchHit
from singleAgent.dm_search import SearchBundle


@dataclass
class EventBrief:
    """附件解析后的事件概述（报告主文档）。"""

    title: str = ""
    summary: str = ""
    entities: List[str] = field(default_factory=list)
    time_range: Optional[str] = None
    locations: List[str] = field(default_factory=list)
    event_types: List[str] = field(default_factory=list)
    source_files: List[str] = field(default_factory=list)
    raw_excerpt: str = ""
    ingest_source: str = "rule"  # rule | llm | degraded

    @property
    def has_attachment_content(self) -> bool:
        return bool(self.raw_excerpt.strip() or self.summary.strip())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["EventBrief"]:
        if not isinstance(data, dict):
            return None
        return cls(
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            entities=[str(x) for x in (data.get("entities") or []) if str(x).strip()],
            time_range=data.get("time_range") or None,
            locations=[
                str(x) for x in (data.get("locations") or []) if str(x).strip()
            ],
            event_types=[
                str(x) for x in (data.get("event_types") or []) if str(x).strip()
            ],
            source_files=[
                str(x) for x in (data.get("source_files") or []) if str(x).strip()
            ],
            raw_excerpt=str(data.get("raw_excerpt") or ""),
            ingest_source=str(data.get("ingest_source") or "rule"),
        )


@dataclass
class InformationNeed:
    id: str
    description: str
    suggested_tables_hint: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    priority: str = "required"  # required | optional
    source: str = "troops"  # troops | knowledge | both

    @classmethod
    def from_dict(cls, data: Dict[str, Any], *, index: int) -> "InformationNeed":
        raw_source = data.get("source")
        source = str(raw_source or "").strip().lower()
        blob = " ".join(
            [
                str(data.get("description") or ""),
                " ".join(str(x) for x in (data.get("keywords") or [])),
            ]
        )
        if source not in {"troops", "knowledge", "both"}:
            if any(k in blob for k in ("条令", "法规", "背景", "知识库", "条例")):
                source = "knowledge"
            else:
                source = "troops"
        return cls(
            id=str(data.get("id") or f"need_{index}"),
            description=str(data.get("description") or "").strip(),
            suggested_tables_hint=[
                str(x)
                for x in (
                    data.get("suggested_tables_hint")
                    or data.get("suggested_tables")
                    or []
                )
                if str(x).strip()
            ],
            keywords=[str(x) for x in (data.get("keywords") or []) if str(x).strip()],
            priority=str(data.get("priority") or "required"),
            source=source,
        )


@dataclass
class SectionPlan:
    section_id: str
    title: str
    guidance: str
    needs: List[InformationNeed] = field(default_factory=list)
    status: str = "pending"  # pending | retrieving | satisfied | partial | skipped

    @classmethod
    def from_dict(cls, data: Dict[str, Any], *, index: int) -> "SectionPlan":
        needs_raw = data.get("needs") or []
        needs = [
            InformationNeed.from_dict(n, index=i + 1)
            for i, n in enumerate(needs_raw)
            if isinstance(n, dict)
        ]
        return cls(
            section_id=str(data.get("section_id") or f"s{index}"),
            title=str(data.get("title") or f"第{index}节").strip(),
            guidance=str(data.get("guidance") or "").strip(),
            needs=needs,
            status=str(data.get("status") or "pending"),
        )


@dataclass
class SectionEvidence:
    section_id: str
    section_title: str
    hits: List[McpSearchHit] = field(default_factory=list)
    queries_executed: List[str] = field(default_factory=list)
    satisfied_needs: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    kb_summary: str = ""
    status: str = "pending"  # satisfied | partial | skipped | empty
    retries_used: int = 0

    @property
    def empty(self) -> bool:
        return not self.hits and not (self.kb_summary or "").strip()


@dataclass
class ReportTraceStep:
    stage: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReportAgentResult:
    sections: List[SectionPlan] = field(default_factory=list)
    evidences: List[SectionEvidence] = field(default_factory=list)
    search_bundle: SearchBundle = field(default_factory=SearchBundle)
    evidence_summary_text: str = ""
    writer_context_block: str = ""
    trace: List[ReportTraceStep] = field(default_factory=list)
    retrieval_count: int = 0
    kb_summary: str = ""
    write_per_section: bool = False