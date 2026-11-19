"""正文溯源：标N 目录 + 流式检测 (标N) 时发出 tip SSE。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Generator, Iterable, List, Optional, Set

from singleAgent.config import (
    KNOWLEDGE_BASE_MAX_SUMMARY_LINES,
    MAX_SEARCH_SUMMARY_LINES,
)
from singleAgent.dm_search import SearchBundle
from singleAgent.knowledge_base import KnowledgeBundle, chunk_ref_id
from singleAgent.sse import emit_tip

_MARKER_RE = re.compile(r"\(标(\d+)\)")
# 可能跨 chunk 的未完成前缀：`(`, `(标`, `(标12`
_INCOMPLETE_PREFIX_RE = re.compile(r"\(标?\d*$")

CITATION_PROMPT_RULES = """【溯源标注】
1. 引用下列材料时，在对应事实句末使用半角括号标注，格式严格为 (标N)，例如 (标1)。
2. 只使用下方已列出的编号；不要编造未列出的标号。
3. 无对应材料依据处不要标注；同一条材料可多次引用同一编号。
4. 不要输出单独的参考文献列表或脚注区。
5. 编号以下方材料目录为准；展示层会按正文出现顺序重排，无需自行按段落重编号。"""


@dataclass(frozen=True)
class CitationTip:
    label: str  # 标1
    block_id: str
    dataset_name: str
    document_name: str
    text: str
    published_at: str = ""

    @property
    def marker(self) -> str:
        return f"({self.label})"


def _trim(text: str, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def _dm_block_id(row: Any) -> str:
    platform = getattr(row, "platform", "") or ""
    url = getattr(row, "url", None) or ""
    title = (getattr(row, "title_or_content", None) or "")[:240]
    raw = f"dm|{platform}|{url}|{title}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _format_publish_time(row: Any) -> str:
    pub = getattr(row, "publish_time", None)
    if isinstance(pub, datetime):
        return pub.strftime("%Y-%m-%d")
    if pub:
        return str(pub)[:32]
    return ""


def _split_dm_title_body(
    raw: str,
    *,
    title_limit: int = 48,
    body_limit: int = 280,
) -> tuple[str, str]:
    """达梦只有 title_or_content：尽量拆出原标题与切片正文，避免弹窗标题=正文。"""
    text = (raw or "").strip()
    if not text:
        return "（无标题）", "（无正文）"

    compact = re.sub(r"[ \t]+", " ", text)
    compact = re.sub(r"\n+", "\n", compact).strip()
    one_line = re.sub(r"\s+", " ", compact)

    title = one_line
    m = re.match(rf"^(.{{8,{title_limit}}}?)[。！？；]", one_line)
    if m:
        title = m.group(1).strip()
    elif len(one_line) > title_limit:
        cut = one_line[:title_limit]
        break_at = max(cut.rfind("，"), cut.rfind("、"), cut.rfind(" "), cut.rfind("："))
        title = (cut[:break_at] if break_at >= 12 else cut).strip()
    title = title.strip("《》\"'“” ") or one_line[:title_limit]

    body = _trim(compact.replace("\n", " "), body_limit)
    return title, body or "（无正文）"


def _compose_tip_text(*, published_at: str, slice_text: str) -> str:
    """前端 tip 固定字段无独立日期位：把创建日期写进 text，悬停可见。"""
    date_line = f"创建日期：{(published_at or '').strip() or '待考'}"
    body = (slice_text or "").strip() or "（无正文）"
    return f"{date_line}\n{body}"


def build_citation_tips(
    *,
    search_bundle: Optional[SearchBundle] = None,
    kb_bundle: Optional[KnowledgeBundle] = None,
    max_dm: int = MAX_SEARCH_SUMMARY_LINES,
    max_kb: int = KNOWLEDGE_BASE_MAX_SUMMARY_LINES,
) -> List[CitationTip]:
    """从达梦命中 + 知识库切片构建统一 标N 表（达梦在前）。

    前端固定映射（不可改字段名）：
    - ``document_name`` ← 原标题
    - ``dataset_name`` ← 库/知识库
    - ``text`` ← 创建日期 + 切片内容
    """
    tips: List[CitationTip] = []
    n = 1

    hits = list((search_bundle.hits if search_bundle else [])[: max(0, int(max_dm))])
    for row in hits:
        platform = (getattr(row, "platform", None) or "未知平台").strip()
        raw = (getattr(row, "title_or_content", None) or "").strip()
        published = _format_publish_time(row)
        title, body = _split_dm_title_body(raw)
        tips.append(
            CitationTip(
                label=f"标{n}",
                block_id=_dm_block_id(row),
                dataset_name=f"达梦库/{platform}",
                document_name=title,
                text=_compose_tip_text(published_at=published, slice_text=body),
                published_at=published,
            )
        )
        n += 1

    chunks = list((kb_bundle.chunks if kb_bundle else [])[: max(0, int(max_kb))])
    for ch in chunks:
        dataset = (ch.dataset_id or "").strip() or "知识库"
        doc = (ch.document_keyword or "").strip() or "未知文档"
        tips.append(
            CitationTip(
                label=f"标{n}",
                block_id=chunk_ref_id(ch),
                dataset_name=f"知识库/{dataset}",
                document_name=doc,
                text=_compose_tip_text(
                    published_at="",
                    slice_text=_trim(ch.content, 280) or "（无正文）",
                ),
            )
        )
        n += 1

    return tips


def format_citation_catalog(tips: Iterable[CitationTip]) -> str:
    items = list(tips)
    if not items:
        return ""
    lines = [CITATION_PROMPT_RULES, "", "可用溯源编号："]
    for tip in items:
        date_part = f"｜{tip.published_at}" if tip.published_at else ""
        lines.append(
            f"- {tip.marker} {tip.dataset_name}{date_part}｜"
            f"{tip.document_name}｜{tip.text}"
        )
    return "\n".join(lines)


def tips_by_label(tips: Iterable[CitationTip]) -> Dict[str, CitationTip]:
    return {t.label: t for t in tips}


def citation_tip_payload(
    tip: CitationTip,
    *,
    display_label: Optional[str] = None,
) -> Dict[str, Any]:
    """与 SSE ``event: tip`` 的 ``delta`` 对齐。"""
    payload: Dict[str, Any] = {
        "type": "text_delta",
        "label": display_label or tip.label,
        "block_id": tip.block_id,
        "dataset_name": tip.dataset_name,
        "document_name": tip.document_name,
        "text": tip.text,
    }
    if tip.published_at:
        payload["published_at"] = tip.published_at
    return payload


def citation_tip_event(
    tip: CitationTip,
    *,
    display_label: Optional[str] = None,
    index: int = 0,
) -> Dict[str, Any]:
    """与 SSE ``event: tip`` 的 ``data`` 同构，供历史回放复用实时渲染。"""
    return {
        "type": "tip",
        "index": index,
        "delta": citation_tip_payload(tip, display_label=display_label),
    }


def citation_history_item(
    tip: CitationTip,
    *,
    display_label: Optional[str] = None,
) -> Dict[str, Any]:
    """对齐认知攻防等模块写入平台的 ``metadata.citations[]``（历史页读这个，不读 content）。"""
    label = display_label or tip.label
    dataset = (tip.dataset_name or "").strip()
    kb_id = ""
    if dataset.startswith("知识库/"):
        kb_id = dataset.split("/", 1)[1].strip()
    source_type = "knowledge_base" if dataset.startswith("知识库") else "public_opinion"
    return {
        "id": label,
        "label": label,
        "marker": f"({label})",
        "block_id": tip.block_id,
        "dataset_name": dataset or None,
        "document_name": tip.document_name,
        "text": tip.text,
        "title": tip.document_name,
        "content": tip.text,
        "kb_id": kb_id or None,
        "kb_name": None,
        "created_at": tip.published_at or None,
        "source_type": source_type,
    }


def remap_citation_markers_by_appearance(text: str) -> str:
    """按正文中首次出现顺序，将 (标N) 重排为 (标1)(标2)…

    检索目录编号（达梦在前）仅约束模型可用集合；展示序号按阅读顺序。
    """
    if not text or "(标" not in text:
        return text
    old_to_new: Dict[str, str] = {}
    seq = 0

    def _repl(match: re.Match[str]) -> str:
        nonlocal seq
        old = f"标{match.group(1)}"
        mapped = old_to_new.get(old)
        if mapped is None:
            seq += 1
            mapped = f"标{seq}"
            old_to_new[old] = mapped
        return f"({mapped})"

    return _MARKER_RE.sub(_repl, text)


def _split_hold_incomplete(buf: str) -> tuple[str, str]:
    """把可能未完成的 `(标…` 前缀留在 hold 区。"""
    idx = buf.rfind("(")
    if idx < 0:
        return buf, ""
    suffix = buf[idx:]
    if _MARKER_RE.fullmatch(suffix):
        return buf, ""
    if _INCOMPLETE_PREFIX_RE.fullmatch(suffix):
        return buf[:idx], suffix
    return buf, ""


class CitationTipEmitter:
    """扫描流式正文：按首次出现顺序重映射 (标N)，并插入 tip 帧。

    ``feed`` / ``flush`` 产出 ``("text", s)`` 或 ``("tip", sse_frame)``。
    tip 的 ``label`` 为重映射后的展示序号（正文顺序）。
    """

    def __init__(self, tips: List[CitationTip]):
        self._by_label = tips_by_label(tips)
        self._emitted: Set[str] = set()  # 原始目录 label，各发一次 tip
        self._old_to_new: Dict[str, str] = {}
        self._seq = 0
        self._hold = ""

    def _display_label(self, catalog_label: str) -> str:
        mapped = self._old_to_new.get(catalog_label)
        if mapped is None:
            self._seq += 1
            mapped = f"标{self._seq}"
            self._old_to_new[catalog_label] = mapped
        return mapped

    def feed(self, text: str) -> Generator[tuple[str, str], None, None]:
        if not text:
            return
        if not self._by_label:
            # 无目录时仍按出现顺序重排正文角标，便于观感一致
            buf = self._hold + text
            ready, self._hold = _split_hold_incomplete(buf)
            if ready:
                yield from self._scan(ready, allow_unknown_remap=True)
            return

        buf = self._hold + text
        ready, self._hold = _split_hold_incomplete(buf)
        if ready:
            yield from self._scan(ready, allow_unknown_remap=True)

    def flush(self) -> Generator[tuple[str, str], None, None]:
        if not self._hold:
            return
        leftover = self._hold
        self._hold = ""
        yield from self._scan(leftover, allow_unknown_remap=True)

    def _emitted_ordered(self) -> List[tuple[str, CitationTip]]:
        ordered = sorted(
            self._emitted,
            key=lambda lab: int(self._old_to_new.get(lab, "标0")[1:] or 0),
        )
        out: List[tuple[str, CitationTip]] = []
        for catalog_label in ordered:
            tip = self._by_label.get(catalog_label)
            if tip is None:
                continue
            display = self._old_to_new.get(catalog_label) or tip.label
            out.append((display, tip))
        return out

    def emitted_payloads(self) -> List[Dict[str, Any]]:
        """正文实际用过的 tip，形状与 SSE ``event: tip`` 的 data 同构。"""
        return [
            citation_tip_event(tip, display_label=display, index=index)
            for index, (display, tip) in enumerate(self._emitted_ordered())
        ]

    def emitted_citations(self) -> List[Dict[str, Any]]:
        """平台历史 ``metadata.citations``，对齐认知攻防等可被前端历史页绑定的结构。"""
        return [
            citation_history_item(tip, display_label=display)
            for display, tip in self._emitted_ordered()
        ]

    def _scan(
        self,
        ready: str,
        *,
        allow_unknown_remap: bool,
    ) -> Generator[tuple[str, str], None, None]:
        pos = 0
        for match in _MARKER_RE.finditer(ready):
            start, end = match.start(), match.end()
            if start > pos:
                yield ("text", ready[pos:start])
            catalog_label = f"标{match.group(1)}"
            tip = self._by_label.get(catalog_label)
            if tip is None and not allow_unknown_remap:
                yield ("text", match.group(0))
                pos = end
                continue
            display = self._display_label(catalog_label)
            yield ("text", f"({display})")
            pos = end
            if tip is None or catalog_label in self._emitted:
                continue
            self._emitted.add(catalog_label)
            yield (
                "tip",
                emit_tip(
                    label=display,
                    block_id=tip.block_id,
                    dataset_name=tip.dataset_name,
                    document_name=tip.document_name,
                    text=tip.text,
                    published_at=tip.published_at,
                ),
            )
        if pos < len(ready):
            yield ("text", ready[pos:])
