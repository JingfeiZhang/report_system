"""达梦多轮检索：规则抽词 + 早停，缓解 LIKE 整串 miss。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from loguru import logger

from singleAgent.config import (
    LIMIT_PER_TABLE,
    MAX_SEARCH_SUMMARY_LINES,
    MAX_SEARCH_TERMS,
    TARGET_HITS,
)
from singleAgent.dm_mcp_search import (
    is_mcp_sample_all,
    is_mcp_search_enabled,
    search_term_mcp,
)
from utils.topic_anchor import extract_topic_tokens


@dataclass
class SearchBundle:
    terms_tried: List[str] = field(default_factory=list)
    hits: List[Any] = field(default_factory=list)
    empty: bool = True
    summary_text: str = ""

    @property
    def total_count(self) -> int:
        return len(self.hits)


def plan_search_terms(search_text: str, max_terms: int = MAX_SEARCH_TERMS) -> List[str]:
    """从用户文本抽取检索词，优先 2~8 字，避免整句 LIKE。"""
    if not (search_text or "").strip():
        return []

    raw = extract_topic_tokens(search_text.strip())
    picked: List[str] = []
    seen: Set[str] = set()

    def add(term: str) -> None:
        term = term.strip()
        if len(term) < 2 or len(term) > 12 or term in seen:
            return
        seen.add(term)
        picked.append(term)

    for token in raw:
        if 2 <= len(token) <= 8:
            add(token)
    for token in raw:
        add(token)

    return picked[:max_terms]


def _hit_key(row: Any) -> Tuple[str, str, str]:
    title = (getattr(row, "title_or_content", None) or "")[:120]
    platform = getattr(row, "platform", "") or ""
    url = getattr(row, "url", None) or ""
    return platform, title, url


def _format_hit_line(row: Any, index: int) -> str:
    platform = getattr(row, "platform", "") or "未知平台"
    text = (getattr(row, "title_or_content", None) or "").strip().replace("\n", " ")
    if len(text) > 100:
        text = text[:100] + "…"
    pub = getattr(row, "publish_time", None)
    if isinstance(pub, datetime):
        time_str = pub.strftime("%Y-%m-%d")
    else:
        time_str = "日期待考"
    return f"{index}. [{platform}] {time_str} | {text or '（无正文）'}"


def _build_summary_sample_all(merged: List[Any]) -> str:
    lines = [
        f"全库采样 {len(merged)} 条（已跳过关键词过滤，供模型自行筛选）：",
    ]
    for i, row in enumerate(merged[:MAX_SEARCH_SUMMARY_LINES], 1):
        lines.append(_format_hit_line(row, i))
    if len(merged) > MAX_SEARCH_SUMMARY_LINES:
        lines.append(f"… 另有 {len(merged) - MAX_SEARCH_SUMMARY_LINES} 条未列出")
    return "\n".join(lines)


def _build_summary(tried: List[str], merged: List[Any]) -> str:
    if not merged:
        return "库内暂无与主题匹配的舆情数据。"
    if is_mcp_sample_all():
        return _build_summary_sample_all(merged)
    lines = [
        f"共检索 {len(tried)} 个关键词（{'、'.join(tried)}），命中 {len(merged)} 条：",
    ]
    for i, row in enumerate(merged[:MAX_SEARCH_SUMMARY_LINES], 1):
        lines.append(_format_hit_line(row, i))
    if len(merged) > MAX_SEARCH_SUMMARY_LINES:
        lines.append(f"… 另有 {len(merged) - MAX_SEARCH_SUMMARY_LINES} 条未列出")
    return "\n".join(lines)


def _open_search_db():
    try:
        from InsightEngine.tools.search import MediaCrawlerDB
    except ImportError as e:
        logger.error(f"无法加载 MediaCrawlerDB: {e}")
        return None
    return MediaCrawlerDB()


def search_one_term(
    db: Any,
    term: str,
    merged: List[Any],
    seen_keys: Set[Tuple[str, str, str]],
    *,
    target_hits: Optional[int] = None,
) -> int:
    """检索单个关键词并合并去重，返回本轮新增条数。"""
    hit_limit = TARGET_HITS if target_hits is None else max(1, int(target_hits))
    before = len(merged)

    if is_mcp_search_enabled():
        try:
            rows = search_term_mcp(term)
        except Exception as e:
            logger.warning(f"MCP 检索「{term}」失败: {e}")
            return 0
        for row in rows:
            key = _hit_key(row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(row)
            if len(merged) >= hit_limit:
                break
        return len(merged) - before

    try:
        resp = db.search_topic_globally(term, limit_per_table=LIMIT_PER_TABLE)
    except Exception as e:
        logger.warning(f"检索「{term}」失败: {e}")
        return 0

    for row in resp.results or []:
        key = _hit_key(row)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(row)
        if len(merged) >= hit_limit:
            break
    return len(merged) - before


def finalize_search_bundle(tried: List[str], merged: List[Any]) -> SearchBundle:
    if not tried:
        return SearchBundle(empty=True, summary_text="库内暂无与主题匹配的舆情数据。")
    if not merged:
        return SearchBundle(
            terms_tried=tried,
            hits=[],
            empty=True,
            summary_text="库内暂无与主题匹配的舆情数据。",
        )
    return SearchBundle(
        terms_tried=tried,
        hits=merged,
        empty=False,
        summary_text=_build_summary(tried, merged),
    )


def multi_round_search(
    search_text: str,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> SearchBundle:
    """按词多轮 search_topic_globally，合并去重，够数早停。

    cancel_check：每轮检索前后调用；用于中途「新对话」尽快抛 GenerationCancelled，
    避免 closers=0 时仍卡在长 LIKE 上占死流线程。
    """
    terms = plan_search_terms(search_text)
    if not terms:
        return SearchBundle(empty=True, summary_text="库内暂无与主题匹配的舆情数据。")

    def _check() -> None:
        if cancel_check:
            cancel_check()

    db = None
    if not is_mcp_search_enabled():
        _check()
        db = _open_search_db()
        if db is None:
            return SearchBundle(
                terms_tried=terms,
                empty=True,
                summary_text="数据库检索模块不可用。",
            )

    merged: List[Any] = []
    seen_keys: Set[Tuple[str, str, str]] = set()

    if is_mcp_sample_all():
        tried = ["[全库采样]"]
        _check()
        search_one_term(db, terms[0], merged, seen_keys)
        return finalize_search_bundle(tried, merged)

    tried: List[str] = []

    for term in terms:
        _check()
        tried.append(term)
        search_one_term(db, term, merged, seen_keys)
        _check()
        if len(merged) >= TARGET_HITS:
            break

    return finalize_search_bundle(tried, merged)
