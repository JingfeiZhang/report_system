"""event2 专用：经 MCP/达梦直读业务库上下文（替代 mock + MediaCrawlerDB 舆情检索范式）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger

from mcp_server_dm.schema_search import (
    SchemaSearchConfig,
    SchemaSearchHit,
    discover_tables,
    load_schema_search_config,
    resolve_dm_connect_kwargs,
    sample_table,
)
from singleAgent.dm_mcp_search import McpSearchHit, mcp_payload_to_hits


@dataclass
class McpContextBundle:
    """MCP 达梦上下文（非舆情 mock 检索产物）。"""

    schema: str = ""
    topic: str = ""
    tables_total: int = 0
    tables_sampled: int = 0
    rows: List[McpSearchHit] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    empty: bool = True
    summary_text: str = ""
    mode: str = "mcp_context"

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _default_deny_tables() -> Set[str]:
    raw = (os.getenv("DM_MCP_CONTEXT_DENY_TABLES") or "AGENTS").strip()
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


def _default_deny_prefixes() -> Tuple[str, ...]:
    raw = (
        os.getenv("DM_MCP_CONTEXT_DENY_PREFIXES")
        or os.getenv("DM_MCP_SAMPLE_DENY_PREFIXES")
        or "DICT_,SYS_,TMP_,TEMP_"
    ).strip()
    if not raw:
        return ()
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def filter_context_tables(
    tables: Sequence[str],
    *,
    config: SchemaSearchConfig,
    deny_tables: Optional[Set[str]] = None,
    deny_prefixes: Optional[Tuple[str, ...]] = None,
) -> List[str]:
    """去掉 MCP 配置表/字典表，保留业务表。"""
    deny_tbl = deny_tables if deny_tables is not None else _default_deny_tables()
    deny_pre = deny_prefixes if deny_prefixes is not None else _default_deny_prefixes()
    picked: List[str] = []
    for name in tables:
        upper = name.upper()
        if config.table_allowlist is not None and upper not in config.table_allowlist:
            continue
        if upper in deny_tbl:
            continue
        if deny_pre and any(upper.startswith(p) for p in deny_pre):
            continue
        if config.table_deny_prefixes and any(
            upper.startswith(p) for p in config.table_deny_prefixes
        ):
            continue
        picked.append(name)
        if len(picked) >= config.max_tables:
            break
    return picked


def _schema_hits_to_rows(
    schema: str,
    hits: Sequence[SchemaSearchHit],
    *,
    topic: str,
) -> List[McpSearchHit]:
    payload = {
        "schema": schema,
        "hits": [h.to_dict() for h in hits],
    }
    label = (topic or "上下文").strip()[:24]
    return mcp_payload_to_hits(payload, keyword=f"[MCP·{label}]")


def _build_summary(
    *,
    schema: str,
    topic: str,
    tables_total: int,
    tables_sampled: int,
    rows: Sequence[McpSearchHit],
    errors: Sequence[str],
) -> str:
    lines = [
        f"数据源：达梦 schema「{schema}」（event2 MCP 直读，非 mock 舆情检索）",
    ]
    if topic.strip():
        lines.append(f"讨论主题：{topic.strip()}")
    lines.append(
        f"业务表 {tables_total} 张，已采样 {tables_sampled} 张，共 {len(rows)} 条记录片段："
    )
    for idx, row in enumerate(rows[:15], 1):
        text = (row.title_or_content or "").strip().replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"{idx}. [{row.platform}] {text or '（无摘要）'}")
    if len(rows) > 15:
        lines.append(f"… 另有 {len(rows) - 15} 条未列出")
    if errors:
        lines.append("备注：" + "；".join(str(e) for e in errors[:3]))
    return "\n".join(lines)


def fetch_mcp_context(
    topic: str = "",
    *,
    config: Optional[SchemaSearchConfig] = None,
) -> McpContextBundle:
    """拉取达梦 schema 业务表采样，作为 LLM 上下文（不做舆情式关键词检索）。"""
    cfg = config or load_schema_search_config()
    target = max(
        1,
        int(
            os.getenv(
                "DM_MCP_CONTEXT_MAX_ROWS",
                os.getenv("DM_MCP_SAMPLE_ALL_TARGET_HITS", "80"),
            )
        ),
    )
    rows_per_table = max(
        1,
        int(os.getenv("DM_MCP_SAMPLE_ROWS_PER_TABLE", str(cfg.limit_per_table))),
    )
    errors: List[str] = []
    schema_hits: List[SchemaSearchHit] = []

    try:
        import dmPython

        conn = dmPython.connect(**resolve_dm_connect_kwargs())
    except Exception as exc:
        msg = f"达梦连接失败: {exc}"
        logger.error(msg)
        return McpContextBundle(
            schema=cfg.schema,
            topic=topic,
            errors=[msg],
            empty=True,
            summary_text=f"达梦库不可用（{exc}）",
        )

    try:
        all_tables = discover_tables(conn, cfg.schema)
        tables = filter_context_tables(all_tables, config=cfg)
        if not all_tables:
            errors.append(f"schema={cfg.schema} 未发现任何表")
        elif not tables:
            errors.append(
                f"schema={cfg.schema} 共 {len(all_tables)} 张表，过滤后无业务表可采样"
            )

        scanned = 0
        for table_name in tables:
            if len(schema_hits) >= target:
                break
            scanned += 1
            table_hits = sample_table(
                conn,
                cfg.schema,
                table_name,
                limit=rows_per_table,
            )
            if not table_hits and scanned <= 3:
                errors.append(f"空表或不可读: {table_name}")
            schema_hits.extend(table_hits)
            if len(schema_hits) >= target:
                schema_hits = schema_hits[:target]
                break

        rows = _schema_hits_to_rows(cfg.schema, schema_hits, topic=topic)
        summary = _build_summary(
            schema=cfg.schema,
            topic=topic,
            tables_total=len(tables),
            tables_sampled=scanned,
            rows=rows,
            errors=errors,
        )
        logger.info(
            "MCP context schema={} tables={}/{} rows={} topic={}",
            cfg.schema,
            scanned,
            len(tables),
            len(rows),
            (topic or "")[:40],
        )
        return McpContextBundle(
            schema=cfg.schema,
            topic=topic,
            tables_total=len(tables),
            tables_sampled=scanned,
            rows=rows,
            errors=errors,
            empty=not rows,
            summary_text=summary,
        )
    except Exception as exc:
        msg = f"达梦采样失败: {exc}"
        logger.error(msg)
        return McpContextBundle(
            schema=cfg.schema,
            topic=topic,
            errors=[msg],
            empty=True,
            summary_text=msg,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def check_dm_context_available(
    *,
    config: Optional[SchemaSearchConfig] = None,
) -> Tuple[bool, str]:
    """event2 前置检查：能否连上达梦并看到 schema 表。"""
    cfg = config or load_schema_search_config()
    try:
        import dmPython

        conn = dmPython.connect(**resolve_dm_connect_kwargs())
        try:
            all_tables = discover_tables(conn, cfg.schema)
            biz = filter_context_tables(all_tables, config=cfg)
            if not all_tables:
                return False, f"schema {cfg.schema} 下无表"
            if not biz:
                return (
                    False,
                    f"schema {cfg.schema} 有 {len(all_tables)} 张表但无可用业务表",
                )
            return True, f"ok schema={cfg.schema} biz_tables={len(biz)}"
        finally:
            conn.close()
    except Exception as exc:
        return False, str(exc)
