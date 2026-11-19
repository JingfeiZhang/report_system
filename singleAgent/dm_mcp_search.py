"""event2 通过 MCP / 内联 schema_search 做全表检索。"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from mcp_server_dm.schema_search import (
    SchemaSearchConfig,
    expand_keywords,
    is_sample_all_mode,
    load_schema_search_config,
    sample_schema_globally,
    search_schema_globally,
)


@dataclass
class McpSearchHit:
    """与 MediaCrawlerDB QueryResult 字段对齐，供 dm_search / citations 复用。"""

    platform: str
    content_type: str = "db_row"
    title_or_content: str = ""
    author_nickname: Optional[str] = None
    url: Optional[str] = None
    publish_time: Optional[datetime] = None
    source_table: str = ""
    source_keyword: Optional[str] = None


def _dm_search_backend() -> str:
    return (os.getenv("DM_SEARCH_BACKEND") or "legacy").strip().lower()


def is_mcp_search_enabled() -> bool:
    return _dm_search_backend() in {"mcp", "mcp_inline", "troops"}


def is_mcp_sample_all() -> bool:
    """MCP 模式下是否跳过 LIKE，直接全库采样。"""
    return is_mcp_search_enabled() and is_sample_all_mode()


_SAMPLE_ALL_CACHE: Dict[str, List[McpSearchHit]] = {}
_LAST_SAMPLE_DIAG: Dict[str, Any] = {}


def clear_mcp_sample_all_cache() -> None:
    _SAMPLE_ALL_CACHE.clear()
    _LAST_SAMPLE_DIAG.clear()


def get_last_mcp_sample_diag() -> Dict[str, Any]:
    return dict(_LAST_SAMPLE_DIAG)


def _mcp_url() -> str:
    return (os.getenv("DM_MCP_URL") or "").strip()


def _sample_on_empty() -> bool:
    raw = (os.getenv("DM_MCP_SAMPLE_ON_EMPTY") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _parse_publish_time(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip().replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _pick_publish_time_from_row(row: Dict[str, Any]) -> Optional[datetime]:
    for key, value in row.items():
        upper = str(key).upper()
        if any(marker in upper for marker in ("TIME", "DATE", "CREATED", "UPDATED", "PUBLISH")):
            parsed = _parse_publish_time(value)
            if parsed:
                return parsed
    return None


def mcp_payload_to_hits(payload: Dict[str, Any], *, keyword: str) -> List[McpSearchHit]:
    hits: List[McpSearchHit] = []
    schema = str(payload.get("schema") or "")
    for item in payload.get("hits") or []:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "")
        row = item.get("row") if isinstance(item.get("row"), dict) else {}
        snippet = str(item.get("snippet") or "").strip()
        if not snippet and row:
            snippet = " | ".join(f"{k}={v}" for k, v in list(row.items())[:4])
        hits.append(
            McpSearchHit(
                platform=f"{schema}.{table}" if schema and table else (table or schema or "达梦"),
                title_or_content=snippet or "（无摘要）",
                url=f"{schema}.{table}" if schema and table else None,
                publish_time=_pick_publish_time_from_row(row),
                source_table=table,
                source_keyword=keyword,
            )
        )
    return hits


def _search_inline(keyword: str, config: Optional[SchemaSearchConfig] = None) -> List[McpSearchHit]:
    payload = search_schema_globally(keyword, config=config)
    if payload.get("errors"):
        logger.warning("MCP inline search errors: {}", payload.get("errors"))
    logger.info(
        "MCP LIKE schema={} keyword={} tables_scanned={} hits={}",
        payload.get("schema"),
        keyword,
        payload.get("tables_scanned"),
        len(payload.get("hits") or []),
    )
    return mcp_payload_to_hits(payload, keyword=keyword)


def _sample_inline(
    config: Optional[SchemaSearchConfig] = None,
    *,
    full_scan: bool = False,
) -> List[McpSearchHit]:
    global _LAST_SAMPLE_DIAG
    payload = sample_schema_globally(config=config, full_scan=full_scan)
    _LAST_SAMPLE_DIAG = {
        "schema": payload.get("schema"),
        "mode": payload.get("mode"),
        "tables_total": payload.get("tables_total"),
        "tables_scanned": payload.get("tables_scanned"),
        "hits": len(payload.get("hits") or []),
        "errors": list(payload.get("errors") or []),
    }
    if payload.get("errors"):
        logger.warning("MCP sample errors: {}", payload.get("errors"))
    label = "全库采样" if payload.get("mode") == "sample_all" else "库表采样"
    hit_count = len(payload.get("hits") or [])
    log_fn = logger.error if hit_count == 0 else logger.info
    log_fn(
        "MCP {} schema={} tables_scanned={}/{} hits={} errors={}",
        label,
        payload.get("schema"),
        payload.get("tables_scanned"),
        payload.get("tables_total"),
        hit_count,
        payload.get("errors"),
    )
    return mcp_payload_to_hits(payload, keyword=f"[{label}]")


async def _search_via_mcp_client_async(keyword: str, schema: str) -> List[McpSearchHit]:
    from fastmcp import Client

    url = _mcp_url()
    async with Client(url) as client:
        result = await client.call_tool(
            "search_schema_globally_tool",
            {"keyword": keyword, "schema": schema},
        )
    text = ""
    if hasattr(result, "content") and result.content:
        block = result.content[0]
        text = getattr(block, "text", "") or str(block)
    elif isinstance(result, str):
        text = result
    else:
        text = str(result)
    payload = json.loads(text)
    return mcp_payload_to_hits(payload, keyword=keyword)


def _search_via_mcp_client(keyword: str, schema: str) -> List[McpSearchHit]:
    return asyncio.run(_search_via_mcp_client_async(keyword, schema))


def _sample_all_target_hits(cfg: SchemaSearchConfig) -> int:
    raw = (os.getenv("DM_MCP_SAMPLE_ALL_TARGET_HITS") or "").strip()
    if raw:
        return max(1, int(raw))
    return max(cfg.target_hits, 60)


def search_term_mcp(
    keyword: str,
    *,
    config: Optional[SchemaSearchConfig] = None,
) -> List[McpSearchHit]:
    """全表检索：sample_all 模式直接全库采样；否则原词 → LIKE →（可选）采样回退。"""
    cfg = config or load_schema_search_config()

    if is_mcp_sample_all():
        target = _sample_all_target_hits(cfg)
        cache_key = (
            f"{cfg.schema}|{target}|{cfg.max_tables}|{cfg.limit_per_table}|"
            f"{cfg.table_deny_prefixes}|{cfg.table_allowlist}"
        )
        if cache_key not in _SAMPLE_ALL_CACHE:
            sample_cfg = SchemaSearchConfig(
                schema=cfg.schema,
                limit_per_table=cfg.limit_per_table,
                target_hits=target,
                max_tables=cfg.max_tables,
                table_allowlist=cfg.table_allowlist,
                table_deny_prefixes=cfg.table_deny_prefixes,
                max_text_columns_per_table=cfg.max_text_columns_per_table,
            )
            rows = _sample_inline(sample_cfg, full_scan=True)
            if rows:
                _SAMPLE_ALL_CACHE[cache_key] = rows
            else:
                return []
        return _SAMPLE_ALL_CACHE[cache_key][:target]

    url = _mcp_url()

    terms = expand_keywords(keyword)
    if not terms:
        terms = [keyword] if (keyword or "").strip() else []

    merged: List[McpSearchHit] = []
    seen: set[str] = set()

    def _absorb(rows: List[McpSearchHit]) -> None:
        for row in rows:
            key = f"{row.platform}|{(row.title_or_content or '')[:160]}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
            if len(merged) >= cfg.target_hits:
                return

    for term in terms:
        if len(merged) >= cfg.target_hits:
            break
        if url:
            try:
                rows = _search_via_mcp_client(term, cfg.schema)
            except Exception as exc:
                logger.warning("MCP client 调用失败，回落内联检索: {}", exc)
                rows = _search_inline(term, cfg)
        else:
            rows = _search_inline(term, cfg)
        _absorb(rows)

    if not merged and _sample_on_empty():
        logger.info(
            "MCP LIKE 全 miss（terms={}），启用业务表采样回退 schema={}",
            terms,
            cfg.schema,
        )
        _absorb(_sample_inline(cfg))

    return merged[: cfg.target_hits]
