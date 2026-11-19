"""TROOPS Agent 内联 Tool 执行（共用 schema_search）。"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from mcp_server_dm.schema_search import (
    SchemaSearchConfig,
    describe_table_full,
    execute_readonly_query,
    list_tables_with_comments,
    load_schema_search_config,
    resolve_dm_connect_kwargs,
    sample_table,
)
from singleAgent.troops_agent.sql_guard import guard_sql


def _deny_tables() -> Set[str]:
    raw = (
        os.getenv("DM_MCP_CONTEXT_DENY_TABLES")
        or os.getenv("DM_TROOPS_DENY_TABLES")
        or "AGENTS,AGENT_SESSION"
    ).strip()
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


def _deny_prefixes() -> Tuple[str, ...]:
    raw = (
        os.getenv("DM_MCP_CONTEXT_DENY_PREFIXES")
        or "DICT_,SYS_,TMP_,TEMP_"
    ).strip()
    if not raw:
        return ()
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def _allowlist() -> Optional[Set[str]]:
    raw = (
        os.getenv("DM_TROOPS_TABLE_ALLOWLIST")
        or os.getenv("DM_MCP_TABLE_ALLOWLIST")
        or ""
    ).strip()
    if not raw:
        return None
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


def filter_business_catalog(
    catalog: Sequence[Dict[str, Any]],
    *,
    config: Optional[SchemaSearchConfig] = None,
) -> List[Dict[str, Any]]:
    cfg = config or load_schema_search_config()
    deny_tbl = _deny_tables()
    deny_pre = _deny_prefixes()
    allow = _allowlist() or cfg.table_allowlist
    picked: List[Dict[str, Any]] = []
    for item in catalog:
        name = str(item.get("table") or "")
        upper = name.upper()
        if not name:
            continue
        if name.startswith("##") or name.startswith("#"):
            continue
        if allow is not None and upper not in allow:
            continue
        if upper in deny_tbl:
            continue
        if deny_pre and any(upper.startswith(p) for p in deny_pre):
            continue
        if cfg.table_deny_prefixes and any(
            upper.startswith(p) for p in cfg.table_deny_prefixes
        ):
            continue
        picked.append(dict(item))
        if len(picked) >= cfg.max_tables:
            break
    return picked


class ToolExecutor:
    def __init__(self, conn, schema: str, *, config: Optional[SchemaSearchConfig] = None):
        self.conn = conn
        self.schema = schema.upper()
        self.config = config or load_schema_search_config(schema)

    def list_catalog(self) -> List[Dict[str, Any]]:
        raw = list_tables_with_comments(
            self.conn,
            self.schema,
            max_tables=max(500, self.config.max_tables),
        )
        return filter_business_catalog(raw, config=self.config)

    def describe(self, table: str) -> Dict[str, Any]:
        return describe_table_full(self.conn, self.schema, table)

    def execute(self, sql: str, *, max_rows: Optional[int] = None) -> Dict[str, Any]:
        limit = max_rows or int(os.getenv("TROOPS_AGENT_MAX_ROWS", "50"))
        ok, err, safe_sql = guard_sql(sql, max_rows=limit)
        if not ok:
            return {
                "ok": False,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": err,
                "sql_executed": sql,
            }
        return execute_readonly_query(
            self.conn, self.schema, safe_sql, max_rows=limit
        )

    def sample_rows(self, table: str, limit: int = 3) -> Dict[str, Any]:
        hits = sample_table(
            self.conn,
            self.schema,
            table,
            limit=max(1, limit),
        )
        rows = [h.row for h in hits]
        return {
            "ok": bool(rows),
            "table": table,
            "rows": rows,
            "row_count": len(rows),
            "error": None if rows else "sample empty",
        }


def open_dm_connection():
    import dmPython

    return dmPython.connect(**resolve_dm_connect_kwargs())
