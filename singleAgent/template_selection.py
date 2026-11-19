"""Two-stage report template selection helpers for event2."""

from __future__ import annotations

from typing import Any, Dict, Iterable

from singleAgent.sse import (
    emit_reference_done,
    emit_retrieval_knowledge,
    emit_thinking_done,
    emit_thinking_progress,
)


TEMPLATE_SELECTION_STAGE = "template_selection"
TEMPLATE_GENERATION_STAGE = "template_generation"
TEMPLATE_INTERACTION_STAGES = {
    TEMPLATE_SELECTION_STAGE,
    TEMPLATE_GENERATION_STAGE,
}


def resolve_interaction_stage(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("interaction_stage") or "").strip()


def merge_generation_instruction(original: str, supplemental: str) -> str:
    primary = (original or "").strip()
    extra = (supplemental or "").strip()
    if not primary:
        return extra
    if not extra:
        return primary
    return f"{primary}\n\n补充要求：\n{extra}"


def stream_template_selection(
    templates: Iterable[Any],
    *,
    session_id: str,
    pending_request_id: str,
    expires_at: float,
):
    refs = [template.to_knowledge_ref() for template in templates]
    yield emit_thinking_progress("已保存待生成内容，请选择报告模板")
    yield emit_retrieval_knowledge(
        refs=refs,
        message=f"可选报告版式（共 {len(refs)} 个），请选择后再次发送",
        session_id=session_id,
        progress=len(refs),
    )
    yield emit_thinking_done(
        success=True,
        mode=1,
        interaction_stage="awaiting_template",
        awaiting_template=True,
        pending_request_id=pending_request_id,
        expires_at=expires_at,
        message="等待选择报告模板",
    )
    yield emit_reference_done()


def merge_knowledge_base_ids(*groups: Iterable[Any]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group or []:
            item = str(value).strip()
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged
