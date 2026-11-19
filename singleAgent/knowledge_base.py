"""知识库检索：前端直接传 dataset_id，POST open/retrieval。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import httpx
from loguru import logger

from singleAgent.config import (
    KNOWLEDGE_BASE_API_BASE_URL,
    KNOWLEDGE_BASE_API_KEY,
    KNOWLEDGE_BASE_MAX_SUMMARY_LINES,
    KNOWLEDGE_BASE_TIMEOUT_SEC,
    KNOWLEDGE_BASE_TOP_K,
)

_RETRIEVAL_PATH = "/api/v1/dataplatform/knowledge-base/open/retrieval"


@dataclass
class KnowledgeChunk:
    content: str
    similarity: float = 0.0
    document_keyword: str = ""
    dataset_id: str = ""


@dataclass
class KnowledgeBundle:
    kb_ids: List[str] = field(default_factory=list)
    dataset_ids: List[str] = field(default_factory=list)
    chunks: List[KnowledgeChunk] = field(default_factory=list)
    empty: bool = True
    summary_text: str = ""
    error: str = ""

    @property
    def total_count(self) -> int:
        return len(self.chunks)

    def to_knowledge_refs(self) -> List[Dict[str, Any]]:
        return chunks_to_knowledge_refs(self.chunks)


def chunk_ref_id(chunk: KnowledgeChunk) -> str:
    raw = f"{chunk.document_keyword}|{chunk.dataset_id}|{chunk.content[:240]}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def chunks_to_knowledge_refs(chunks: List[KnowledgeChunk]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for ch in chunks:
        refs.append({
            "id": chunk_ref_id(ch),
            "content": ch.content,
            "score": ch.similarity,
            "source": ch.document_keyword or "未知文档",
            "category": "通用知识",
            "dataset_id": ch.dataset_id or "",
        })
    return refs


def normalize_knowledge_base_ids(
    ids: Optional[List[Union[int, str]]],
) -> List[str]:
    """将请求中的 knowledge_base_ids 规范为 dataset_id 字符串列表（去重保序）。"""
    out: List[str] = []
    seen: set[str] = set()
    for item in ids or []:
        if item is None:
            continue
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _api_base() -> str:
    return (KNOWLEDGE_BASE_API_BASE_URL or "").rstrip("/")


def _retrieval_headers() -> Dict[str, str]:
    key = (KNOWLEDGE_BASE_API_KEY or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _trim_content(text: str, limit: int = 180) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned or "（无正文）"


def _build_summary(
    *,
    dataset_ids: List[str],
    chunks: List[KnowledgeChunk],
) -> str:
    if not chunks:
        if dataset_ids:
            ids = "、".join(dataset_ids)
            return f"已在知识库 {ids} 中检索，暂无与主题匹配的片段。"
        return "未选择知识库。"
    lines = [
        f"共检索 {len(dataset_ids)} 个知识库（ID：{'、'.join(dataset_ids)}），"
        f"命中 {len(chunks)} 条：",
    ]
    for i, ch in enumerate(chunks[:KNOWLEDGE_BASE_MAX_SUMMARY_LINES], 1):
        doc = ch.document_keyword or "未知文档"
        sim = f"{ch.similarity:.2f}" if ch.similarity else "—"
        lines.append(f"{i}. [文档: {doc}] 相似度 {sim} | {_trim_content(ch.content)}")
    if len(chunks) > KNOWLEDGE_BASE_MAX_SUMMARY_LINES:
        lines.append(f"… 另有 {len(chunks) - KNOWLEDGE_BASE_MAX_SUMMARY_LINES} 条未列出")
    return "\n".join(lines)


def _parse_chunks(data: Any) -> List[KnowledgeChunk]:
    if not isinstance(data, dict):
        return []
    raw_chunks = data.get("chunks") or []
    if not isinstance(raw_chunks, list):
        return []
    parsed: List[KnowledgeChunk] = []
    for item in raw_chunks:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        try:
            sim = float(item.get("similarity") or 0)
        except (TypeError, ValueError):
            sim = 0.0
        parsed.append(
            KnowledgeChunk(
                content=content,
                similarity=sim,
                document_keyword=str(item.get("document_keyword") or ""),
                dataset_id=str(item.get("dataset_id") or ""),
            )
        )
    parsed.sort(key=lambda c: c.similarity, reverse=True)
    if KNOWLEDGE_BASE_TOP_K > 0:
        parsed = parsed[:KNOWLEDGE_BASE_TOP_K]
    return parsed


def retrieve(question: str, kb_ids: List[Union[int, str]]) -> KnowledgeBundle:
    """对指定知识库执行 RAG 检索；kb_ids 即为 dataset_id。"""
    q = (question or "").strip()
    dataset_ids = normalize_knowledge_base_ids(
        kb_ids if isinstance(kb_ids, list) else []
    )
    if not dataset_ids:
        return KnowledgeBundle(summary_text="未选择知识库。")

    if not _api_base():
        return KnowledgeBundle(
            kb_ids=dataset_ids,
            dataset_ids=dataset_ids,
            empty=True,
            summary_text="知识库服务未配置（KNOWLEDGE_BASE_API_BASE_URL）。",
            error="missing_base_url",
        )
    if not (KNOWLEDGE_BASE_API_KEY or "").strip():
        return KnowledgeBundle(
            kb_ids=dataset_ids,
            dataset_ids=dataset_ids,
            empty=True,
            summary_text="知识库 API Key 未配置。",
            error="missing_api_key",
        )

    bundle = KnowledgeBundle(kb_ids=dataset_ids, dataset_ids=dataset_ids)

    if not q:
        q = "分析主题相关资料"

    try:
        with httpx.Client(timeout=KNOWLEDGE_BASE_TIMEOUT_SEC) as client:
            resp = client.post(
                f"{_api_base()}{_RETRIEVAL_PATH}",
                headers=_retrieval_headers(),
                json={"question": q, "dataset_ids": dataset_ids},
            )
        if resp.status_code == 401:
            bundle.error = "unauthorized"
            bundle.summary_text = "知识库检索未授权，请检查 KNOWLEDGE_BASE_API_KEY。"
            return bundle
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict) and body.get("code") not in (0, None):
            msg = str(body.get("msg") or "知识库检索失败")
            bundle.error = msg
            bundle.summary_text = f"知识库检索失败：{msg}"
            return bundle
        data = body.get("data") if isinstance(body, dict) else {}
        bundle.chunks = _parse_chunks(data)
        bundle.empty = not bundle.chunks
        bundle.summary_text = _build_summary(
            dataset_ids=dataset_ids,
            chunks=bundle.chunks,
        )
        return bundle
    except httpx.TimeoutException:
        bundle.error = "timeout"
        bundle.summary_text = "知识库检索超时，将仅依据舆情库与案例继续分析。"
        logger.warning("知识库检索超时")
        return bundle
    except Exception as e:
        bundle.error = str(e)
        bundle.summary_text = f"知识库暂不可用（{e}），将仅依据舆情库与案例继续分析。"
        logger.warning(f"知识库检索失败: {e}")
        return bundle
