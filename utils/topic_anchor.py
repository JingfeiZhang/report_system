"""
主题锚定工具：防止 Agent 搜索与总结偏离用户原始 query。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set

GENERIC_QUERY_PATTERN = re.compile(
    r"今天热搜|最近在聊|都在聊|聊什么|聊啥|"
    r"全球人工智能|麦肯锡|世界经济论坛|IEEE|"
    r"AI换脸|GARIA|负责任创新|"
    r"研究概述|深度分析.*查询主题|相关主题研究|深度研究补充",
    re.IGNORECASE,
)

_TOPIC_HINT_EXPANSIONS: List[tuple] = [
    (
        re.compile(r"美伊|伊朗|以伊|美以|以色列|中东|霍尔木兹|特拉维夫|德黑兰|伊拉克|叙利亚"),
        ["伊朗", "以色列", "美伊", "美以", "中东"],
    ),
    (
        re.compile(r"俄乌|乌克兰|俄罗斯|普京"),
        ["乌克兰", "俄罗斯", "俄乌"],
    ),
    (
        re.compile(r"贸易战|关税|特朗普"),
        ["贸易战", "关税", "特朗普"],
    ),
]

_PRIORITY_TOKENS = ["伊朗", "以色列", "美伊", "美以", "中东", "乌克兰", "俄罗斯"]

PROMPT_TOPIC_ANCHOR_SEARCH = """
**【主题锚定 — 最高优先级】**
输入 JSON 中的 `user_query` 是用户原始分析主题，全程不得偏离。
1. `search_query` 必须围绕 `user_query` 及 `topic_anchor_tokens` 设计，不得改为无关热点（如 AI换脸、全球AI发展、麦肯锡、气候、票房等），除非 `user_query` 本身就在讨论这些。
2. 禁止将「今天热搜」「最近在聊什么」「研究概述」等无主题泛词作为唯一或主要检索词。
3. 当段落 title/content 较泛时，以 `user_query` 为准，不得以段落标题更换主题。
4. 中文检索优先使用 2–6 字、便于数据库匹配的网民常用词，可从 `topic_anchor_tokens` 选取。
"""

PROMPT_TOPIC_ANCHOR_SUMMARY = """
**【主题锚定与事实约束 — 最高优先级】**
1. 全文仅分析 `user_query` 主题；不得引入与 `user_query` 无关的第二大议题。
2. 若 `search_results` 为空、或包含「[空库提示]」「未找到结果」「本次搜索未返回」等：只简短说明「库内暂无相关数据，无法展开」，**禁止**编造阅读量、用户数、评论、机构报告或典型案例。
3. 所有数字、引文必须来自 `search_results`；无来源则不写。
4. 若「信息密度/字数」要求与事实约束冲突，以事实约束为准。
"""


def _cjk_ngrams(text: str, min_len: int = 2, max_len: int = 3) -> List[str]:
    if not re.fullmatch(r"[\u4e00-\u9fff]+", text):
        return []
    ngrams: List[str] = []
    for n in range(min_len, max_len + 1):
        for i in range(len(text) - n + 1):
            ngrams.append(text[i : i + n])
    return ngrams


def extract_topic_tokens(user_query: str) -> List[str]:
    """从用户原始 query 提取可用于数据库检索的锚定词。"""
    if not user_query or not user_query.strip():
        return []

    seen: Set[str] = set()
    tokens: List[str] = []

    def add(token: str) -> None:
        token = token.strip()
        if len(token) < 2 or token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for part in re.split(r"[\s，。！？；：、/|（）()\[\]【】]+", user_query.strip()):
        if not part:
            continue
        add(part)
        for ng in _cjk_ngrams(part):
            add(ng)

    for pattern, extras in _TOPIC_HINT_EXPANSIONS:
        if pattern.search(user_query):
            for extra in extras:
                add(extra)

    tokens.sort(key=len, reverse=True)
    return tokens


def is_generic_offtopic_query(search_query: str) -> bool:
    if not search_query:
        return True
    return bool(GENERIC_QUERY_PATTERN.search(search_query))


def is_query_on_topic(search_query: str, user_query: str) -> bool:
    if not search_query or not search_query.strip():
        return False
    if is_generic_offtopic_query(search_query):
        return False
    if not user_query or not user_query.strip():
        return True

    for token in extract_topic_tokens(user_query):
        if token in search_query or token.lower() in search_query.lower():
            return True
    return False


def fallback_search_query(user_query: str, max_terms: int = 3) -> str:
    tokens = extract_topic_tokens(user_query)
    if not tokens:
        return (user_query or "").strip()[:50] or "热点"

    picked: List[str] = []
    for priority in _PRIORITY_TOKENS:
        if priority in tokens and priority not in picked:
            picked.append(priority)
    for token in tokens:
        if token not in picked:
            picked.append(token)
        if len(picked) >= max_terms:
            break
    return " ".join(picked[:max_terms])


def sanitize_search_query(search_query: str, user_query: str) -> str:
    """确保搜索词锚定在用户主题上；偏离时回退到安全检索词。"""
    original = (search_query or "").strip()
    user_query = (user_query or "").strip()
    if not user_query:
        return original
    if original and is_query_on_topic(original, user_query):
        return original
    return fallback_search_query(user_query)


def build_agent_context(user_query: str, title: str, content: str, **extra: Any) -> Dict[str, Any]:
    """搜索/反思节点的标准输入。"""
    payload: Dict[str, Any] = {
        "user_query": user_query,
        "title": title,
        "content": content,
        "topic_anchor_tokens": extract_topic_tokens(user_query),
    }
    payload.update(extra)
    return payload
