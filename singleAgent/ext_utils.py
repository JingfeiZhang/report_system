"""ext 字段语义：案例材料 vs 模板选择（勿混用）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

# 仅表示模板/版式选择
_TEMPLATE_META_KEYS = frozenset({
    "selected_knowledge_ids",
    "report_template_id",
    "report_template",
    "template_id",
    "ismodel",
})

_CASE_CONTENT_KEYS = (
    "content",
    "text",
    "description",
    "case_content",
    "brief",
    "instruction",
    "title",
)


def _value_meaningfully_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def ext_is_nonempty(ext: Optional[Dict[str, Any]]) -> bool:
    """
    ext 是否非空（含任意有意义字段）。
    产品规则：非空 → mode0；空 → 强制 mode1。
    """
    if not isinstance(ext, dict) or not ext:
        return False
    return any(_value_meaningfully_present(v) for v in ext.values())


def ext_has_template_selection(ext: Optional[Dict[str, Any]]) -> bool:
    """右屏勾选后带回的模板选择（selected_knowledge_ids 等）。"""
    if not isinstance(ext, dict) or not ext:
        return False
    for key in ("selected_knowledge_ids", "report_template_id", "report_template", "template_id"):
        val = ext.get(key)
        if isinstance(val, list) and any(
            x is not None and str(x).strip() for x in val
        ):
            return True
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return True
        if isinstance(val, str) and val.strip():
            return True
    return False


def default_instruction_for_template_report(session_topic: str = "") -> str:
    """空 instruction + 已选模板时，用会话主题拼默认生成意图。"""
    topic = (session_topic or "").strip()
    if topic:
        return (
            f"请基于当前会话已有讨论（主题：{topic}），"
            "按所选模板生成完整报告。"
        )
    return "请基于当前会话已有讨论，按所选模板生成完整报告。"


def _file_item_as_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump()
        except Exception:
            pass
    return {
        "name": str(getattr(item, "name", "") or ""),
        "type": str(getattr(item, "type", "") or ""),
        "content": str(getattr(item, "content", "") or ""),
    }


def merge_instruction_with_file_contents(
    instruction: str,
    files: Optional[list] = None,
    *,
    max_chars_per_file: Optional[int] = None,
) -> str:
    """
    将附件解析文本并入用户输入（与 instruction 逻辑等价）。
    伪代码：user_prompt += content
    """
    parts: list[str] = []
    text = (instruction or "").strip()
    if text:
        parts.append(text)

    if max_chars_per_file is None:
        try:
            from singleAgent.config import MAX_ATTACHMENT_CHARS

            max_chars_per_file = int(MAX_ATTACHMENT_CHARS)
        except Exception:
            max_chars_per_file = 2000

    for item in files or []:
        data = _file_item_as_dict(item)
        content = str(data.get("content") or "").strip()
        if not content:
            continue
        limit = max(0, int(max_chars_per_file or 0))
        if limit and len(content) > limit:
            content = content[:limit] + "…（已截断）"
        parts.append(content)

    return "\n\n".join(parts).strip()


def apply_attachment_merge_to_metadata(metadata: Optional[Dict[str, Any]]) -> str:
    """
    在 event2 入口把 files_data[].content 合并进 metadata['instruction']。
    幂等：已合并则直接返回当前 instruction。
    """
    meta = metadata if isinstance(metadata, dict) else {}
    if meta.get("attachments_merged_into_instruction"):
        return str(meta.get("instruction") or "").strip()

    raw = str(meta.get("instruction") or "").strip()
    files = meta.get("files_data") if isinstance(meta.get("files_data"), list) else []
    merged = merge_instruction_with_file_contents(raw, files)
    if raw and merged != raw:
        meta["raw_instruction"] = raw
    meta["instruction"] = merged
    meta["attachments_merged_into_instruction"] = True
    return merged


def ext_has_case_material(ext: Optional[Dict[str, Any]]) -> bool:
    """
    是否携带需重新分析的案例正文。
    仅含 selected_knowledge_ids 等模板字段时返回 False。
    """
    if not isinstance(ext, dict) or not ext:
        return False

    for key in _CASE_CONTENT_KEYS:
        val = ext.get(key)
        if isinstance(val, str) and val.strip():
            return True

    for key, val in ext.items():
        if key in _TEMPLATE_META_KEYS:
            continue
        if isinstance(val, str) and val.strip():
            return True
        if isinstance(val, dict) and val:
            return True
        if isinstance(val, list) and val:
            return True
    return False
