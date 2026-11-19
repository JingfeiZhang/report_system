"""附件 → EventBrief（P1：规则抽取 + 可选 LLM 增强）。"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

from singleAgent.config import MAX_ATTACHMENT_CHARS
from singleAgent.ext_utils import _file_item_as_dict
from singleAgent.report_agent.models import EventBrief

_EVENT_TYPE_KEYWORDS = (
    "部署",
    "机动",
    "演习",
    "冲突",
    "打击",
    "防御",
    "登陆",
    "封锁",
    "侦察",
    "补给",
)

_ENTITY_PATTERNS = (
    re.compile(r"[\u4e00-\u9fff]{2,12}(?:师|旅|团|营|连|舰队|编队|集团军|战区|方面军)"),
    re.compile(r"[\u4e00-\u9fff]{2,8}(?:省|市|区|县|岛|海|湾|港|机场|基地)"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日?"),
    re.compile(r"\d{4}年\d{1,2}月"),
    re.compile(r"第[\d一二三四五六七八九十百千]+[师旅团营]"),
)

_TIME_RANGE_PATTERN = re.compile(
    r"(\d{4}年\d{1,2}月\d{1,2}日?(?:\s*[至到\-—~]\s*\d{4}年\d{1,2}月\d{1,2}日?)?)"
    r"|(\d{4}年\d{1,2}月(?:\s*[至到\-—~]\s*\d{4}年\d{1,2}月)?)"
)

_LLM_SYSTEM = """你是内场事件文档解析助手。用户上传的附件是待分析事件的最新概述主文档。
请从正文中抽取结构化信息，输出唯一 JSON 对象（不要 markdown 代码块）：
{
  "title": "事件标题（短）",
  "summary": "500字以内事件概述，保留时间线要点",
  "entities": ["部队/装备/人物/组织名，最多15个"],
  "time_range": "事件时间范围或null",
  "locations": ["地点，最多8个"],
  "event_types": ["部署/机动/冲突/演习等，最多5个"]
}
只依据正文，不要编造。"""


def _brief_int(name: str, default: int) -> int:
    return max(256, int(os.getenv(name, str(default))))


def _collect_attachment_text(
    metadata: Dict[str, Any],
) -> tuple[str, List[str], List[str]]:
    """返回 (合并正文, 文件名列表, 各段标题)。"""
    files = metadata.get("files_data") if isinstance(metadata.get("files_data"), list) else []
    max_chars = _brief_int("REPORT_EVENT_BRIEF_MAX_CHARS", 12000)
    blocks: List[str] = []
    names: List[str] = []
    headings: List[str] = []

    for i, item in enumerate(files, 1):
        data = _file_item_as_dict(item)
        name = (data.get("name") or f"附件{i}").strip()
        content = str(data.get("content") or "").strip()
        if not content:
            continue
        names.append(name)
        headings.append(name)
        if len(content) > max_chars:
            content = content[:max_chars] + "…（已截断）"
        blocks.append(f"## {name}\n{content}")

    if blocks:
        merged = "\n\n".join(blocks)
        if len(merged) > max_chars:
            merged = merged[:max_chars] + "…（已截断）"
        return merged, names, headings

    # 附件已合并进 instruction 时，用 raw_instruction + 截断 instruction 尾部
    raw = str(metadata.get("raw_instruction") or "").strip()
    instruction = str(metadata.get("instruction") or "").strip()
    fallback = raw or instruction
    if fallback:
        limit = max(max_chars, MAX_ATTACHMENT_CHARS)
        if len(fallback) > limit:
            fallback = fallback[:limit] + "…（已截断）"
        return fallback, [], []
    return "", [], []


def _dedupe_keep_order(items: Sequence[str], *, limit: int = 20) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        text = (item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _extract_entities(text: str) -> List[str]:
    found: List[str] = []
    for pattern in _ENTITY_PATTERNS:
        found.extend(pattern.findall(text))
    # 2~6 字连续中文词（粗抽）
    for m in re.findall(r"[\u4e00-\u9fff]{2,6}", text[:4000]):
        if any(kw in m for kw in ("军", "师", "旅", "舰", "战", "部", "区", "连")):
            found.append(m)
    return _dedupe_keep_order(found, limit=15)


def _extract_locations(text: str) -> List[str]:
    locs: List[str] = []
    for pattern in _ENTITY_PATTERNS[1:2]:
        locs.extend(pattern.findall(text))
    return _dedupe_keep_order(locs, limit=8)


def _extract_event_types(text: str) -> List[str]:
    return _dedupe_keep_order([kw for kw in _EVENT_TYPE_KEYWORDS if kw in text], limit=5)


def _extract_time_range(text: str) -> Optional[str]:
    m = _TIME_RANGE_PATTERN.search(text[:3000])
    if m:
        return (m.group(1) or m.group(2) or "").strip() or None
    return None


def _rule_summary(text: str, *, max_len: int = 600) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_len:
        return cleaned
    # 尽量在句号处截断
    chunk = cleaned[:max_len]
    last_stop = max(chunk.rfind("。"), chunk.rfind("；"), chunk.rfind("\n"))
    if last_stop > max_len // 2:
        return chunk[: last_stop + 1].strip()
    return chunk.strip() + "…"


def _parse_llm_brief(raw: str, *, raw_excerpt: str, source_files: List[str]) -> Optional[EventBrief]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return EventBrief(
        title=str(data.get("title") or "").strip(),
        summary=str(data.get("summary") or "").strip(),
        entities=_dedupe_keep_order([str(x) for x in (data.get("entities") or [])]),
        time_range=(str(data.get("time_range")).strip() if data.get("time_range") else None),
        locations=_dedupe_keep_order([str(x) for x in (data.get("locations") or [])], limit=8),
        event_types=_dedupe_keep_order([str(x) for x in (data.get("event_types") or [])], limit=5),
        source_files=source_files,
        raw_excerpt=raw_excerpt,
        ingest_source="llm",
    )


def _rule_brief(
    *,
    text: str,
    title: str,
    source_files: List[str],
) -> EventBrief:
    summary = _rule_summary(text)
    return EventBrief(
        title=title,
        summary=summary,
        entities=_extract_entities(text),
        time_range=_extract_time_range(text),
        locations=_extract_locations(text),
        event_types=_extract_event_types(text),
        source_files=source_files,
        raw_excerpt=text,
        ingest_source="rule",
    )


def ingest_event_brief(
    *,
    metadata: Optional[Dict[str, Any]] = None,
    topic: str = "",
    llm_cfg: Optional[Dict[str, str]] = None,
) -> EventBrief:
    """从 files_data / instruction 构建 EventBrief。"""
    meta = metadata or {}
    text, source_files, _ = _collect_attachment_text(meta)
    title = (topic or meta.get("resolved_topic") or "未命名事件").strip()

    if not text.strip():
        instruction = str(meta.get("instruction") or "").strip()
        degraded_text = instruction or title
        brief = _rule_brief(
            text=degraded_text,
            title=title,
            source_files=source_files,
        )
        brief.ingest_source = "degraded"
        return brief

    use_llm = os.getenv("REPORT_EVENT_BRIEF_USE_LLM", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if use_llm and llm_cfg and llm_cfg.get("base_url"):
        try:
            from singleAgent.llm import complete_chat

            user_prompt = f"事件标签：{title}\n\n【附件正文】\n{text[: _brief_int('REPORT_EVENT_BRIEF_MAX_CHARS', 12000)]}"
            raw = complete_chat(
                api_key=llm_cfg["api_key"],
                base_url=llm_cfg["base_url"],
                model_name=llm_cfg["model_name"],
                system_prompt=_LLM_SYSTEM,
                user_prompt=user_prompt,
                max_tokens=_brief_int("REPORT_EVENT_BRIEF_LLM_MAX_TOKENS", 1024),
                temperature=0,
                timeout=float(os.getenv("REPORT_EVENT_BRIEF_LLM_TIMEOUT_SEC", "45")),
                check_connectivity=False,
            )
            parsed = _parse_llm_brief(raw, raw_excerpt=text, source_files=source_files)
            if parsed and parsed.summary:
                if not parsed.title:
                    parsed.title = title
                return parsed
            logger.warning("EventBrief LLM 解析失败，降级规则抽取")
        except Exception as exc:
            logger.warning("EventBrief LLM 抽取异常: {}", exc)

    return _rule_brief(text=text, title=title, source_files=source_files)


def build_retrieval_topic(
    brief: Optional[EventBrief],
    *,
    fallback_topic: str = "",
    fallback_instruction: str = "",
) -> str:
    """Troops Agent / MCP 检索用主题：优先 EventBrief。"""
    if brief is None:
        return (fallback_topic or fallback_instruction or "").strip()

    parts: List[str] = []
    if brief.title.strip():
        parts.append(brief.title.strip())
    if brief.summary.strip():
        parts.append(brief.summary.strip()[:800])
    if brief.entities:
        parts.append(" ".join(brief.entities[:20]))
    if brief.locations:
        parts.append(" ".join(brief.locations[:10]))
    if brief.event_types:
        parts.append(" ".join(brief.event_types[:5]))
    if brief.time_range:
        parts.append(str(brief.time_range))

    topic = " ".join(p for p in parts if p).strip()
    if topic:
        return topic[:2000]
    return (fallback_topic or fallback_instruction or brief.raw_excerpt[:500]).strip()


def format_event_brief_block(brief: EventBrief) -> str:
    """Prompt 中【待分析事件概述】块。"""
    lines: List[str] = []
    if brief.title:
        lines.append(f"事件：{brief.title}")
    if brief.time_range:
        lines.append(f"时间：{brief.time_range}")
    if brief.locations:
        lines.append(f"地点：{'、'.join(brief.locations[:8])}")
    if brief.event_types:
        lines.append(f"类型：{'、'.join(brief.event_types)}")
    if brief.entities:
        lines.append(f"关键实体：{'、'.join(brief.entities[:15])}")
    if brief.source_files:
        lines.append(f"来源附件：{'、'.join(brief.source_files)}")
    if brief.summary:
        lines.append(f"\n概述：\n{brief.summary}")
    elif brief.raw_excerpt:
        excerpt = brief.raw_excerpt
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500] + "…"
        lines.append(f"\n正文摘录：\n{excerpt}")
    return "\n".join(lines).strip() or "（未能解析事件概述）"
