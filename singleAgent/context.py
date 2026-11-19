"""拼装 singleAgent 检索文本与 LLM 用户提示。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from singleAgent.config import MAX_ATTACHMENT_CHARS
from singleAgent.ext_utils import ext_has_case_material


@dataclass
class FastContext:
    topic: str
    instruction: str
    search_text: str
    user_prompt: str
    session_id: Optional[str] = None


def _format_ext(ext: Dict[str, Any]) -> str:
    """只抽取案例正文；模板 ID 不算案例。"""
    if not ext_has_case_material(ext):
        return ""
    for key in (
        "content",
        "text",
        "description",
        "case_content",
        "brief",
        "instruction",
        "title",
    ):
        val = ext.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        return json.dumps(ext, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(ext)


def _format_files(files: List[Dict[str, Any]]) -> str:
    if not files:
        return ""
    blocks: List[str] = []
    for i, item in enumerate(files, 1):
        name = (item.get("name") or f"附件{i}").strip()
        ft = (item.get("type") or "unknown").strip()
        content = (item.get("content") or "").strip()
        if not content:
            content = "（附件无文本内容）"
        if len(content) > MAX_ATTACHMENT_CHARS:
            content = content[:MAX_ATTACHMENT_CHARS] + "…（已截断）"
        blocks.append(f"### 附件 {i}: {name}（{ft}）\n\n{content}")
    return "\n\n".join(blocks)


def build_fast_context(
    *,
    topic: str,
    query: str,
    metadata: Optional[Dict[str, Any]] = None,
    search_summary: str = "",
    knowledge_summary: str = "",
    anchor_topic: str = "",
    event_brief: Optional[Any] = None,
    primary_event_doc: bool = False,
) -> FastContext:
    """从 AnalysisEvent 字段构建快模式上下文。"""
    meta = metadata or {}
    instruction = (meta.get("instruction") or "").strip()
    ext = meta.get("ext") if isinstance(meta.get("ext"), dict) else {}
    files = meta.get("files_data") if isinstance(meta.get("files_data"), list) else []

    brief = event_brief
    if brief is None and primary_event_doc:
        try:
            from singleAgent.report_agent.models import EventBrief as EB

            brief = EB.from_dict(meta.get("event_brief"))
        except ImportError:
            brief = None

    use_primary_semantics = primary_event_doc or (
        brief is not None and getattr(brief, "has_attachment_content", False)
    )

    ext_text = _format_ext(ext)
    files_text = ""
    if not use_primary_semantics and not meta.get("attachments_merged_into_instruction"):
        files_text = _format_files(files)

    anchor = (anchor_topic or topic or query or "").strip()
    search_parts: List[str] = []
    if use_primary_semantics and brief is not None:
        from singleAgent.report_agent.event_ingest import build_retrieval_topic

        retrieval = build_retrieval_topic(
            brief,
            fallback_topic=anchor,
            fallback_instruction=instruction,
        )
        if retrieval:
            search_parts.append(retrieval)
    else:
        if anchor:
            search_parts.append(anchor)
        if instruction and instruction != anchor:
            search_parts.append(instruction)
        if ext_text:
            search_parts.append(ext_text)
        if not search_parts:
            search_parts.append(topic or query)
    search_text = "\n".join(search_parts)

    topic_label = "【事件标签】" if use_primary_semantics else "【分析主题】"
    body_parts: List[str] = [f"{topic_label}\n{topic or query}"]
    if use_primary_semantics:
        raw_instr = str(meta.get("raw_instruction") or "").strip()
        if raw_instr:
            body_parts.append(f"【用户输入】\n{raw_instr}")
        elif instruction and not meta.get("attachments_merged_into_instruction"):
            body_parts.append(f"【用户输入】\n{instruction}")
    elif instruction:
        body_parts.append(f"【用户输入】\n{instruction}")

    case_label = "【对照案例】" if use_primary_semantics else "【所选案例】"
    if ext_text:
        body_parts.append(f"{case_label}\n{ext_text}")

    if use_primary_semantics and brief is not None:
        from singleAgent.report_agent.event_ingest import format_event_brief_block

        body_parts.append(
            f"【待分析事件概述】（主文档）\n{format_event_brief_block(brief)}"
        )
    elif files_text:
        body_parts.append(f"【附件材料】\n{files_text}")
    recent_messages = meta.get("conversation_context")
    if isinstance(recent_messages, list) and recent_messages:
        lines: List[str] = []
        for item in recent_messages[-12:]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = "用户" if item.get("role") in ("human", "user") else "助手"
            lines.append(f"{role}：{content}")
        if lines:
            body_parts.append("【当前会话上下文】\n" + "\n".join(lines))
    if search_summary:
        db_label = "【库内辅证】" if use_primary_semantics else "【库内检索摘要】"
        body_parts.append(f"{db_label}\n{search_summary}")
    else:
        empty_db = (
            "库内暂无与事件相关的辅证数据。"
            if use_primary_semantics
            else "库内暂无与主题匹配的舆情数据。"
        )
        body_parts.append(
            f"{'【库内辅证】' if use_primary_semantics else '【库内检索摘要】'}\n{empty_db}"
        )
    if knowledge_summary:
        body_parts.append(f"【知识库检索摘要】\n{knowledge_summary}")
    elif meta.get("knowledge_base_ids"):
        body_parts.append("【知识库检索摘要】\n知识库暂无与主题匹配的片段。")

    return FastContext(
        topic=topic or "分析报告",
        instruction=instruction,
        search_text=search_text,
        user_prompt="\n\n".join(body_parts),
        session_id=meta.get("session_id"),
    )
