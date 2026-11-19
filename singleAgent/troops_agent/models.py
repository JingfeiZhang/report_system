"""TROOPS MCP Agent 数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from singleAgent.dm_mcp_search import McpSearchHit
from singleAgent.dm_search import SearchBundle


@dataclass
class AgentTraceStep:
    stage: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContextBundle:
    schema: str = ""
    topic: str = ""
    selected_tables: List[str] = field(default_factory=list)
    hits: List[McpSearchHit] = field(default_factory=list)
    trace: List[AgentTraceStep] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    empty: bool = True
    summary_text: str = ""

    def to_search_bundle(self) -> SearchBundle:
        label = "[TROOPS Agent]"
        if self.empty:
            return SearchBundle(
                terms_tried=[label],
                hits=[],
                empty=True,
                summary_text=self.summary_text or "达梦 Agent 未返回可用上下文。",
            )
        return SearchBundle(
            terms_tried=[label],
            hits=list(self.hits),
            empty=False,
            summary_text=self.summary_text,
        )
