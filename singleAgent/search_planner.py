"""mode=0 检索规划：两次 LLM（主题 → 检索词）+ 可选补搜轮；失败静默回退。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from singleAgent.config import (
    MAX_SEARCH_TERMS,
    SEARCH_PLAN_MAX_TOKENS,
    SEARCH_PLAN_TEMPERATURE,
    SEARCH_PLAN_TIMEOUT_SEC,
)
from singleAgent.dm_search import plan_search_terms
from singleAgent.llm import complete_chat
from singleAgent.prompts import (
    SEARCH_REFINE_SYSTEM_PROMPT,
    SEARCH_TERMS_SYSTEM_PROMPT,
    TOPIC_EXTRACT_SYSTEM_PROMPT,
    build_search_refine_user_prompt,
    build_search_terms_user_prompt,
    build_topic_extract_user_prompt,
)
from utils.topic_anchor import fallback_search_query

_META_STOPWORDS = frozenset({
    "请", "请生成", "生成", "报告", "分析", "帮我", "一份", "完整", "重新",
    "所选", "模板", "根据", "重新生成", "生成报告", "分析报告", "写一份",
    "出一份", "整理", "汇总", "材料", "文档",
})


@dataclass
class SearchPlan:
    topic: str
    terms: List[str] = field(default_factory=list)
    topic_source: str = "llm"  # llm | fallback | session
    terms_source: str = "llm"  # llm | fallback | refine


class SearchPlanningError(RuntimeError):
    """Raised when strict report planning cannot be completed by the model."""


_PLACEHOLDER_TOPICS = frozenset({
    "未知",
    "未知主题",
    "未命名主题",
    "分析主题",
    "分析报告",
    "未指定",
    "无",
})
_TOPIC_EDGE_PUNCTUATION = " \t\r\n,，.。!！?？:：;；、'\"`~～-—_"


def _strip_code_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = _strip_code_fence(raw)
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


def normalize_topic(topic: str) -> str:
    """Normalize a topic and reject exact placeholder values."""
    raw = str(topic or "").strip()
    t = re.sub(r"\s+", "", raw)

    # Repeat edge cleanup because punctuation can wrap brackets, for example "（未知）。".
    previous = None
    while t and t != previous:
        previous = t
        t = t.strip(_TOPIC_EDGE_PUNCTUATION)
        t = re.sub(r"^[《「『【\(（]+|[》」』】\)）]+$", "", t)
        t = t.strip(_TOPIC_EDGE_PUNCTUATION)
        t = re.sub(r"(的报告|报告|的分析|分析)$", "", t)
    if t in _PLACEHOLDER_TOPICS:
        logger.warning(
            f"topic_validation=rejected reason=placeholder value={t[:40]}"
        )
        return ""
    if len(t) < 2:
        if raw:
            logger.warning("topic_validation=rejected reason=too_short")
        return ""
    return t[:40]


# Backward-compatible name used by existing local tests.
_clean_topic = normalize_topic


def _normalize_terms(
    terms: List[Any],
    *,
    topic: str,
    exclude: Optional[Set[str]] = None,
    max_terms: int = MAX_SEARCH_TERMS,
) -> List[str]:
    exclude = exclude or set()
    picked: List[str] = []
    seen: Set[str] = set()

    for item in terms:
        if not isinstance(item, str):
            continue
        term = item.strip().replace(" ", "")
        if len(term) < 2 or len(term) > 12:
            continue
        if term in _META_STOPWORDS or term in exclude:
            continue
        # 元话术子串（粗过滤）
        if any(s in term for s in ("生成", "报告", "模板", "请生")):
            continue
        if term in seen:
            continue
        seen.add(term)
        picked.append(term)
        if len(picked) >= max_terms:
            break

    if picked:
        return picked

    # 静默兜底：仅用主题的规则词，不用用户整句 instruction
    fb = fallback_search_query(topic, max_terms=max_terms).split()
    for term in fb:
        term = term.strip()
        if 2 <= len(term) <= 12 and term not in exclude and term not in seen:
            seen.add(term)
            picked.append(term)
        if len(picked) >= max_terms:
            break
    if not picked:
        picked = plan_search_terms(topic, max_terms=max_terms)
    return [t for t in picked if t not in exclude][:max_terms]


def extract_topic_via_llm(
    *,
    instruction: str,
    session_topic: str,
    llm_cfg: Dict[str, str],
    conversation_context: Optional[List[Dict[str, str]]] = None,
    check_connectivity: bool = True,
    strict_context: bool = False,
) -> tuple[str, str]:
    """返回 (topic, source)。"""
    valid_session_topic = normalize_topic(session_topic)
    try:
        raw = complete_chat(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            model_name=llm_cfg["model_name"],
            system_prompt=TOPIC_EXTRACT_SYSTEM_PROMPT,
            user_prompt=build_topic_extract_user_prompt(
                instruction=instruction,
                session_topic=valid_session_topic,
                conversation_context=conversation_context,
            ),
            max_tokens=SEARCH_PLAN_MAX_TOKENS,
            temperature=SEARCH_PLAN_TEMPERATURE,
            timeout=SEARCH_PLAN_TIMEOUT_SEC,
            check_connectivity=check_connectivity,
        )
        data = _parse_json_object(raw)
        topic = normalize_topic(str((data or {}).get("topic") or ""))
        if topic:
            return topic, "llm"
        logger.warning(f"主题抽取解析失败，原始输出: {(raw or '')[:160]}")
    except Exception as e:
        if strict_context:
            raise SearchPlanningError("无法根据对话上下文确定报告事件") from e
        logger.warning(f"主题抽取 LLM 失败，使用兜底: {e}")

    if strict_context:
        raise SearchPlanningError("主题抽取模型未返回有效事件")

    # 兜底：会话主题 → 规则抽词首个/截断 instruction
    if valid_session_topic:
        return valid_session_topic, "session"
    fb_terms = plan_search_terms(instruction, max_terms=1)
    if fb_terms:
        fallback_topic = normalize_topic(fb_terms[0])
        if fallback_topic:
            return fallback_topic, "fallback"
    cleaned = normalize_topic(instruction)
    if cleaned:
        return cleaned[:20], "fallback"
    raise SearchPlanningError("无法从当前指令或会话中确定有效分析主题")


def plan_terms_via_llm(
    *,
    topic: str,
    instruction: str,
    llm_cfg: Dict[str, str],
    check_connectivity: bool = False,
    max_terms: int = MAX_SEARCH_TERMS,
    strict_context: bool = False,
) -> tuple[List[str], str]:
    cap = max(1, int(max_terms))
    try:
        raw = complete_chat(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            model_name=llm_cfg["model_name"],
            system_prompt=SEARCH_TERMS_SYSTEM_PROMPT,
            user_prompt=build_search_terms_user_prompt(
                topic=topic,
                instruction=instruction,
            ),
            max_tokens=SEARCH_PLAN_MAX_TOKENS,
            temperature=SEARCH_PLAN_TEMPERATURE,
            timeout=SEARCH_PLAN_TIMEOUT_SEC,
            check_connectivity=check_connectivity,
        )
        data = _parse_json_object(raw)
        terms_raw = (data or {}).get("terms") if data else None
        if isinstance(terms_raw, list):
            terms = _normalize_terms(terms_raw, topic=topic, max_terms=cap)
            if terms:
                return terms, "llm"
        logger.warning(f"检索词解析失败，原始输出: {(raw or '')[:160]}")
    except Exception as e:
        if strict_context:
            raise SearchPlanningError("无法根据已确定事件生成检索词") from e
        logger.warning(f"检索词 LLM 失败，使用兜底: {e}")

    if strict_context:
        raise SearchPlanningError("检索词规划模型未返回有效检索词")
    return _normalize_terms([], topic=topic, max_terms=cap), "fallback"


def refine_terms_via_llm(
    *,
    topic: str,
    used_terms: List[str],
    hit_summary: str,
    llm_cfg: Dict[str, str],
    check_connectivity: bool = False,
) -> tuple[List[str], str]:
    try:
        raw = complete_chat(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            model_name=llm_cfg["model_name"],
            system_prompt=SEARCH_REFINE_SYSTEM_PROMPT,
            user_prompt=build_search_refine_user_prompt(
                topic=topic,
                used_terms=used_terms,
                hit_summary=hit_summary,
            ),
            max_tokens=SEARCH_PLAN_MAX_TOKENS,
            temperature=SEARCH_PLAN_TEMPERATURE,
            timeout=SEARCH_PLAN_TIMEOUT_SEC,
            check_connectivity=check_connectivity,
        )
        data = _parse_json_object(raw)
        terms_raw = (data or {}).get("terms") if data else None
        if isinstance(terms_raw, list):
            if not terms_raw:
                return [], "llm"
            terms = _normalize_terms(
                terms_raw,
                topic=topic,
                exclude=set(used_terms),
                max_terms=min(3, MAX_SEARCH_TERMS),
            )
            return terms, "llm"
        logger.warning(f"补搜词解析失败，原始输出: {(raw or '')[:160]}")
    except Exception as e:
        logger.warning(f"补搜词 LLM 失败，跳过本轮补搜: {e}")
    return [], "fallback"


def build_initial_search_plan(
    *,
    instruction: str,
    session_topic: str,
    llm_cfg: Dict[str, str],
    max_terms: Optional[int] = None,
    conversation_context: Optional[List[Dict[str, str]]] = None,
    strict_context: bool = False,
) -> SearchPlan:
    """物理两次调用：主题 → 检索词。"""
    topic, topic_source = extract_topic_via_llm(
        instruction=instruction,
        session_topic=session_topic,
        conversation_context=conversation_context,
        llm_cfg=llm_cfg,
        check_connectivity=True,
        strict_context=strict_context,
    )
    term_cap = max_terms if max_terms is not None else MAX_SEARCH_TERMS
    terms, terms_source = plan_terms_via_llm(
        topic=topic,
        instruction=instruction,
        llm_cfg=llm_cfg,
        check_connectivity=False,
        max_terms=term_cap,
        strict_context=strict_context,
    )
    return SearchPlan(
        topic=topic,
        terms=terms,
        topic_source=topic_source,
        terms_source=terms_source,
    )
