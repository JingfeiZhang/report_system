"""Report Agent 子模块（避免 __init__ 重导出 orchestrator，防止与 event2_retrieval 循环导入）。"""

from singleAgent.report_agent.event_ingest import (
    build_retrieval_topic,
    format_event_brief_block,
    ingest_event_brief,
)
from singleAgent.report_agent.models import (
    EventBrief,
    InformationNeed,
    ReportAgentResult,
    SectionEvidence,
    SectionPlan,
)

__all__ = [
    "EventBrief",
    "InformationNeed",
    "SectionPlan",
    "SectionEvidence",
    "ReportAgentResult",
    "ingest_event_brief",
    "build_retrieval_topic",
    "format_event_brief_block",
]
