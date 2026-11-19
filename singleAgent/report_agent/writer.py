"""Report Agent 成稿：全文一次写 / 分节流式写 / 成稿润色。"""

from __future__ import annotations

import os
from typing import List, Optional

from singleAgent.dm_search import SearchBundle
from singleAgent.report_agent.models import SectionEvidence, SectionPlan
from singleAgent.citations import CitationTip, build_citation_tips, format_citation_catalog


def _env_flag(name: str, default: str = "true") -> bool:
    raw = (os.getenv(name) or default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def write_per_section_enabled() -> bool:
    return _env_flag("REPORT_AGENT_WRITE_PER_SECTION", "true")


def polish_enabled() -> bool:
    """成稿后轻量润色（统一版式）；默认开启。"""
    return _env_flag("REPORT_AGENT_POLISH", "true")


def polish_max_tokens(default: int = 4096) -> int:
    try:
        return max(512, int(os.getenv("REPORT_AGENT_POLISH_MAX_TOKENS") or str(default)))
    except ValueError:
        return default


def section_citation_tips(
    *,
    evidence: SectionEvidence,
    global_tips: List[CitationTip],
) -> List[CitationTip]:
    """从全局 tip 中筛出与本节 hits 相关的 tip（按 platform/正文前缀粗匹配）。"""
    if not evidence.hits or not global_tips:
        return []
    keys = set()
    for hit in evidence.hits:
        platform = (hit.platform or "").strip()
        text = (hit.title_or_content or "").strip()[:80]
        keys.add((platform, text))
    matched: List[CitationTip] = []
    for tip in global_tips:
        ds = tip.dataset_name or ""
        doc = tip.document_name or ""
        body = tip.text or ""
        for platform, text in keys:
            if platform and platform in ds:
                matched.append(tip)
                break
            if text and (text in doc or text in body):
                matched.append(tip)
                break
    # 去重保序
    seen = set()
    out: List[CitationTip] = []
    for tip in matched:
        if tip.label in seen:
            continue
        seen.add(tip.label)
        out.append(tip)
    return out


def build_global_citation_tips(
    *,
    search_bundle: SearchBundle,
    kb_bundle=None,
) -> List[CitationTip]:
    return build_citation_tips(
        search_bundle=search_bundle,
        kb_bundle=kb_bundle,
    )


def catalog_for_section(
    tips: List[CitationTip],
    *,
    evidence: Optional[SectionEvidence] = None,
    global_tips: Optional[List[CitationTip]] = None,
) -> str:
    if evidence is not None and global_tips is not None:
        tips = section_citation_tips(evidence=evidence, global_tips=global_tips) or tips
    return format_citation_catalog(tips)


def pair_sections_evidences(
    sections: List[SectionPlan],
    evidences: List[SectionEvidence],
) -> List[tuple[SectionPlan, SectionEvidence]]:
    ev_map = {e.section_id: e for e in evidences}
    paired: List[tuple[SectionPlan, SectionEvidence]] = []
    for sec in sections:
        ev = ev_map.get(sec.section_id) or SectionEvidence(
            section_id=sec.section_id,
            section_title=sec.title,
            status="skipped",
            gaps=["无证据包"],
        )
        paired.append((sec, ev))
    return paired
