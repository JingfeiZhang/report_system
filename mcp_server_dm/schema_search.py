"""
全 schema 只读检索：枚举表 → 文本列 LIKE → 结构化命中。

供 MCP server_multi 与 BettaFish event2（DM_SEARCH_BACKEND=mcp）共用。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("mcp_schema_search")

_TEXT_TYPE_MARKERS = (
    "CHAR",
    "CLOB",
    "TEXT",
    "STRING",
)
_DATE_TYPE_MARKERS = ("DATE", "TIME", "TIMESTAMP")
_DATE_NAME_MARKERS = (
    "TIME",
    "DATE",
    "CREATED",
    "UPDATED",
    "PUBLISH",
    "TS",
    "DAY",
)


@dataclass
class SchemaSearchConfig:
    schema: str
    limit_per_table: int = 5
    target_hits: int = 20
    max_tables: int = 200
    table_allowlist: Optional[Set[str]] = None
    table_deny_prefixes: Tuple[str, ...] = ()
    max_text_columns_per_table: int = 8


@dataclass
class SchemaSearchHit:
    schema: str
    table: str
    row: Dict[str, Any]
    matched_columns: List[str] = field(default_factory=list)
    snippet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "table": self.table,
            "row": self.row,
            "matched_columns": self.matched_columns,
            "snippet": self.snippet,
        }


def resolve_dm_connect_kwargs() -> Dict[str, Any]:
    """MCP 环境变量优先，否则回落 BettaFish DB_*（对齐 InsightEngine dmPython 连接参数）。"""
    host = os.getenv("DM_HOST") or os.getenv("DB_HOST") or "localhost"
    port = int(os.getenv("DM_PORT") or os.getenv("DB_PORT") or "5236")
    user = os.getenv("DM_USERNAME") or os.getenv("DB_USER") or "SYSDBA"
    password = os.getenv("DM_PASSWORD") or os.getenv("DB_PASSWORD") or "SYSDBA"
    return {
        "user": user,
        "password": password,
        "server": host,
        "port": port,
        "autoCommit": True,
        "local_code": 1,
    }


def load_schema_search_config(schema: Optional[str] = None) -> SchemaSearchConfig:
    schema_name = (
        schema
        or os.getenv("DM_MCP_SCHEMA")
        or os.getenv("DM_CURRENT_SCHEMA")
        or os.getenv("DB_NAME")
        or "TROOPS_2_ENHANCED"
    ).strip().upper()

    allow_raw = (os.getenv("DM_MCP_TABLE_ALLOWLIST") or "").strip()
    allowlist: Optional[Set[str]] = None
    if allow_raw:
        allowlist = {t.strip().upper() for t in allow_raw.split(",") if t.strip()}

    deny_raw = (os.getenv("DM_MCP_TABLE_DENY_PREFIXES") or "").strip()
    deny_prefixes: Tuple[str, ...] = ()
    if deny_raw:
        deny_prefixes = tuple(p.strip().upper() for p in deny_raw.split(",") if p.strip())

    return SchemaSearchConfig(
        schema=schema_name,
        limit_per_table=int(os.getenv("DM_MCP_LIMIT_PER_TABLE", "5")),
        target_hits=int(os.getenv("DM_MCP_TARGET_HITS", "20")),
        max_tables=int(os.getenv("DM_MCP_MAX_TABLES", "200")),
        table_allowlist=allowlist,
        table_deny_prefixes=deny_prefixes,
        max_text_columns_per_table=int(os.getenv("DM_MCP_MAX_TEXT_COLUMNS", "8")),
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_text_column(data_type: str) -> bool:
    upper = (data_type or "").upper()
    return any(marker in upper for marker in _TEXT_TYPE_MARKERS)


def _is_date_column(name: str, data_type: str) -> bool:
    upper_type = (data_type or "").upper()
    if any(marker in upper_type for marker in _DATE_TYPE_MARKERS):
        return True
    upper_name = (name or "").upper()
    return any(marker in upper_name for marker in _DATE_NAME_MARKERS)


def _serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


def _row_to_dict(columns: Sequence[str], row: Sequence[Any]) -> Dict[str, Any]:
    return {
        str(col): _serialize_value(val)
        for col, val in zip(columns, row)
    }


def _build_snippet(row: Dict[str, Any], matched_columns: Sequence[str], max_len: int = 240) -> str:
    parts: List[str] = []
    for col in matched_columns:
        val = row.get(col)
        if val is None:
            continue
        text = str(val).strip().replace("\n", " ")
        if not text:
            continue
        parts.append(f"{col}={text}")
    snippet = " | ".join(parts) if parts else str(row)[:max_len]
    if len(snippet) > max_len:
        return snippet[: max_len - 1] + "…"
    return snippet


def discover_tables(conn, schema: str) -> List[str]:
    schema_upper = schema.upper()
    cursor = conn.cursor()
    queries = (
        (
            "SELECT TABLE_NAME FROM ALL_TABLES "
            f"WHERE OWNER = {_sql_literal(schema_upper)} AND TABLE_TYPE = 'TABLE' "
            "ORDER BY TABLE_NAME"
        ),
        (
            "SELECT TABLE_NAME FROM ALL_TABLES "
            f"WHERE OWNER = {_sql_literal(schema_upper)} "
            "ORDER BY TABLE_NAME"
        ),
        (
            "SELECT TABLE_NAME FROM DBA_TABLES "
            f"WHERE OWNER = {_sql_literal(schema_upper)} "
            "ORDER BY TABLE_NAME"
        ),
        (
            "SELECT TABLE_NAME FROM USER_TABLES ORDER BY TABLE_NAME"
        ),
    )
    try:
        for sql in queries:
            try:
                cursor.execute(sql)
                names = [str(row[0]) for row in cursor.fetchall()]
                if names:
                    return names
            except Exception as exc:
                logger.debug("discover_tables fallback failed (%s): %s", sql[:48], exc)
                continue
        return []
    finally:
        cursor.close()


def get_table_columns(conn, schema: str, table_name: str) -> List[Dict[str, Any]]:
    schema_upper = schema.upper()
    table_upper = table_name.upper()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, COLUMN_ID "
            "FROM ALL_TAB_COLUMNS "
            f"WHERE TABLE_NAME = {_sql_literal(table_upper)} "
            f"AND OWNER = {_sql_literal(schema_upper)} "
            "ORDER BY COLUMN_ID"
        )
        rows = cursor.fetchall()
        return [
            {
                "name": str(row[0]),
                "type": str(row[1]) if row[1] is not None else "",
                "length": row[2],
            }
            for row in rows
        ]
    finally:
        cursor.close()


def filter_tables(tables: Sequence[str], config: SchemaSearchConfig) -> List[str]:
    picked: List[str] = []
    for name in tables:
        upper = name.upper()
        if config.table_allowlist is not None and upper not in config.table_allowlist:
            continue
        if config.table_deny_prefixes and any(
            upper.startswith(prefix) for prefix in config.table_deny_prefixes
        ):
            continue
        picked.append(name)
        if len(picked) >= config.max_tables:
            break
    return picked


def _set_schema(cursor, schema: str) -> None:
    schema_upper = schema.upper()
    for sql in (
        f"SET SCHEMA {schema_upper}",
        f'ALTER SESSION SET CURRENT_SCHEMA = "{schema_upper}"',
        f"ALTER SESSION SET CURRENT_SCHEMA = {schema_upper}",
    ):
        try:
            cursor.execute(sql)
            return
        except Exception:
            continue


def _qualified_table(schema: str, table_name: str) -> str:
    return f'{_quote_ident(schema.upper())}.{_quote_ident(table_name)}'


def _fetch_sample_rows(
    cursor,
    schema: str,
    table_name: str,
    *,
    limit: int,
) -> Tuple[List[str], List[Sequence[Any]]]:
    """尝试多种达梦 SQL 写法采样（与 server_multi 已验证路径对齐）。"""
    schema_upper = schema.upper()
    sql_candidates = (
        f"SELECT * FROM {table_name} WHERE ROWNUM <= {int(limit)}",
        f'SELECT * FROM "{table_name}" WHERE ROWNUM <= {int(limit)}',
        f"SELECT * FROM {_qualified_table(schema_upper, table_name)} WHERE ROWNUM <= {int(limit)}",
    )
    last_exc: Optional[Exception] = None
    for sql in sql_candidates:
        try:
            cursor.execute(sql)
            if not cursor.description:
                continue
            col_names = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            if rows:
                return col_names, rows
            return col_names, rows
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    return [], []


def search_table(
    conn,
    schema: str,
    table_name: str,
    keyword: str,
    *,
    limit: int,
    columns_meta: Optional[List[Dict[str, Any]]] = None,
    max_text_columns: int = 8,
) -> List[SchemaSearchHit]:
    if not keyword.strip():
        return []

    meta = columns_meta if columns_meta is not None else get_table_columns(conn, schema, table_name)
    text_cols = [c["name"] for c in meta if _is_text_column(c.get("type", ""))]
    if not text_cols:
        return []
    text_cols = text_cols[:max_text_columns]

    like_term = _escape_like(keyword.strip())
    like_literal = _sql_literal(f"%{like_term}%")
    where = " OR ".join(
        f"{_quote_ident(col)} LIKE {like_literal} ESCAPE '\\'" for col in text_cols
    )
    sql = (
        f"SELECT * FROM {_quote_ident(table_name)} "
        f"WHERE ({where}) AND ROWNUM <= {int(limit)}"
    )

    cursor = conn.cursor()
    try:
        _set_schema(cursor, schema.upper())
        cursor.execute(sql)
        if not cursor.description:
            return []
        col_names = [desc[0] for desc in cursor.description]
        hits: List[SchemaSearchHit] = []
        for row in cursor.fetchall():
            row_dict = _row_to_dict(col_names, row)
            matched = [
                col
                for col in text_cols
                if keyword.lower() in str(row_dict.get(col, "")).lower()
            ]
            hits.append(
                SchemaSearchHit(
                    schema=schema.upper(),
                    table=table_name,
                    row=row_dict,
                    matched_columns=matched or list(text_cols),
                    snippet=_build_snippet(row_dict, matched or text_cols),
                )
            )
        return hits
    except Exception as exc:
        logger.warning("search_table %s.%s keyword=%s failed: %s", schema, table_name, keyword, exc)
        return []
    finally:
        cursor.close()


def sample_table(
    conn,
    schema: str,
    table_name: str,
    *,
    limit: int,
) -> List[SchemaSearchHit]:
    """无关键词：采样表前 N 行（验收期主题相关但 LIKE 词对不上时的回退）。"""
    cursor = conn.cursor()
    try:
        _set_schema(cursor, schema.upper())
        col_names, rows = _fetch_sample_rows(
            cursor, schema, table_name, limit=limit
        )
        if not col_names:
            return []
        hits: List[SchemaSearchHit] = []
        for row in rows:
            row_dict = _row_to_dict(col_names, row)
            snippet = _build_snippet(row_dict, col_names[:6])
            hits.append(
                SchemaSearchHit(
                    schema=schema.upper(),
                    table=table_name,
                    row=row_dict,
                    matched_columns=list(col_names[:6]),
                    snippet=f"[库表采样] {snippet}",
                )
            )
        return hits
    except Exception as exc:
        logger.warning("sample_table %s.%s failed: %s", schema, table_name, exc)
        return []
    finally:
        cursor.close()


def expand_keywords(keyword: str, *, max_terms: int = 8) -> List[str]:
    """把复合中文词切成更短片段，提高 LIKE 命中率。

    例：「部队部署」→ 部队部署、部队、部署
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    out: List[str] = []

    def add(term: str) -> None:
        term = term.strip()
        if len(term) < 2 or term in out:
            return
        out.append(term)

    add(kw)
    if len(kw) >= 4:
        add(kw[:2])
        add(kw[2:4])
        if len(kw) > 4:
            add(kw[-2:])
            # 滑动 2 字窗（限制数量）
            for i in range(0, min(len(kw) - 1, 6)):
                add(kw[i : i + 2])
    elif len(kw) == 3:
        add(kw[:2])
        add(kw[1:])
    return out[:max_terms]


def is_sample_all_mode() -> bool:
    """是否跳过 LIKE，直接全库采样（DM_MCP_MODE=sample_all 或 DM_MCP_BYPASS_KEYWORD=1）。"""
    mode = (os.getenv("DM_MCP_MODE") or "").strip().lower()
    if mode in {"sample_all", "full", "bypass", "sample"}:
        return True
    if mode in {"like", "keyword", ""}:
        if mode == "like":
            return False
        bypass = (os.getenv("DM_MCP_BYPASS_KEYWORD") or "").strip().lower()
        return bypass in {"1", "true", "yes", "on"}
    return False


def sample_schema_globally(
    *,
    config: Optional[SchemaSearchConfig] = None,
    conn: Any = None,
    prefer_skip_dict: bool = True,
    full_scan: bool = False,
) -> Dict[str, Any]:
    """对 schema 内表采样若干行。full_scan=True 时尽量多表、多行（跳过关键词过滤层）。"""
    cfg = config or load_schema_search_config()
    owns_conn = conn is None
    errors: List[str] = []
    hits: List[SchemaSearchHit] = []

    full_scan = full_scan or is_sample_all_mode()

    if full_scan:
        sample_tables_cap = cfg.max_tables
        deny_env = os.getenv("DM_MCP_SAMPLE_DENY_PREFIXES")
        if deny_env is not None and deny_env.strip():
            sample_deny = tuple(
                p.strip().upper() for p in deny_env.split(",") if p.strip()
            )
        else:
            sample_deny = cfg.table_deny_prefixes
        rows_per_table = int(
            os.getenv("DM_MCP_SAMPLE_ROWS_PER_TABLE", str(cfg.limit_per_table))
        )
        row_limit = max(1, rows_per_table)
        effective_target = cfg.target_hits
        raw_all_target = (os.getenv("DM_MCP_SAMPLE_ALL_TARGET_HITS") or "").strip()
        if raw_all_target:
            effective_target = max(1, int(raw_all_target))
        elif effective_target < 40:
            effective_target = max(effective_target, 60)
    else:
        effective_target = cfg.target_hits
        sample_tables_cap = int(os.getenv("DM_MCP_SAMPLE_TABLES", "40"))
        sample_deny = tuple(
            p.strip().upper()
            for p in (os.getenv("DM_MCP_SAMPLE_DENY_PREFIXES") or "DICT_").split(",")
            if p.strip()
        )
        if not prefer_skip_dict:
            sample_deny = cfg.table_deny_prefixes
        row_limit = max(1, min(cfg.limit_per_table, 3))

    try:
        if conn is None:
            import dmPython

            conn = dmPython.connect(**resolve_dm_connect_kwargs())

        sample_cfg = SchemaSearchConfig(
            schema=cfg.schema,
            limit_per_table=cfg.limit_per_table,
            target_hits=effective_target,
            max_tables=min(cfg.max_tables, sample_tables_cap)
            if not full_scan
            else sample_tables_cap,
            table_allowlist=cfg.table_allowlist,
            table_deny_prefixes=sample_deny or cfg.table_deny_prefixes,
            max_text_columns_per_table=cfg.max_text_columns_per_table,
        )
        tables = filter_tables(discover_tables(conn, cfg.schema), sample_cfg)
        if not tables:
            errors.append(
                f"schema={cfg.schema} 未发现可采样表（请检查 DM_MCP_SCHEMA / DB 账号权限）"
            )
        scanned = 0
        sample_failures = 0
        for table_name in tables:
            if len(hits) >= effective_target:
                break
            scanned += 1
            table_hits = sample_table(
                conn,
                cfg.schema,
                table_name,
                limit=row_limit,
            )
            if not table_hits:
                sample_failures += 1
                if sample_failures <= 3:
                    errors.append(f"sample miss: {table_name}")
            hits.extend(table_hits)
            if len(hits) >= effective_target:
                hits = hits[: effective_target]
                break

        if scanned > 0 and not hits:
            errors.append(
                f"已扫描 {scanned}/{len(tables)} 张表但采样均为空（检查 SET SCHEMA / 表权限）"
            )

        mode_label = "sample_all" if full_scan else "sample"
        return {
            "schema": cfg.schema,
            "keyword": "__SAMPLE__",
            "mode": mode_label,
            "hits": [h.to_dict() for h in hits[: effective_target]],
            "tables_scanned": scanned,
            "tables_total": len(tables),
            "errors": errors,
        }
    except Exception as exc:
        return {
            "schema": cfg.schema,
            "keyword": "__SAMPLE__",
            "mode": "sample",
            "hits": [],
            "tables_scanned": 0,
            "errors": [str(exc)],
        }
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def search_schema_globally(
    keyword: str,
    *,
    config: Optional[SchemaSearchConfig] = None,
    conn: Any = None,
) -> Dict[str, Any]:
    """在 schema 下遍历表做 LIKE 检索，返回结构化结果。"""
    cfg = config or load_schema_search_config()
    owns_conn = conn is None
    errors: List[str] = []
    hits: List[SchemaSearchHit] = []

    if not (keyword or "").strip():
        return {
            "schema": cfg.schema,
            "keyword": keyword,
            "hits": [],
            "tables_scanned": 0,
            "errors": ["empty keyword"],
        }

    try:
        if conn is None:
            import dmPython

            conn = dmPython.connect(**resolve_dm_connect_kwargs())

        tables = filter_tables(discover_tables(conn, cfg.schema), cfg)
        scanned = 0
        for table_name in tables:
            if len(hits) >= cfg.target_hits:
                break
            scanned += 1
            try:
                table_hits = search_table(
                    conn,
                    cfg.schema,
                    table_name,
                    keyword,
                    limit=cfg.limit_per_table,
                    max_text_columns=cfg.max_text_columns_per_table,
                )
                hits.extend(table_hits)
            except Exception as exc:
                errors.append(f"{table_name}: {exc}")
            if len(hits) >= cfg.target_hits:
                hits = hits[: cfg.target_hits]
                break

        return {
            "schema": cfg.schema,
            "keyword": keyword,
            "hits": [h.to_dict() for h in hits[: cfg.target_hits]],
            "tables_scanned": scanned,
            "tables_total": len(tables),
            "errors": errors,
        }
    except Exception as exc:
        return {
            "schema": cfg.schema,
            "keyword": keyword,
            "hits": [],
            "tables_scanned": 0,
            "errors": [str(exc)],
        }
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def list_tables_with_comments(
    conn,
    schema: str,
    *,
    max_tables: int = 500,
) -> List[Dict[str, Any]]:
    """返回 schema 下表名、表注释、估计行数（NUM_ROWS 可能过期）。"""
    schema_upper = schema.upper()
    cursor = conn.cursor()
    items: List[Dict[str, Any]] = []
    sql_variants = (
        (
            "SELECT t.TABLE_NAME, c.COMMENTS, t.NUM_ROWS "
            "FROM ALL_TABLES t "
            "LEFT JOIN ALL_TAB_COMMENTS c "
            f"ON t.OWNER = c.OWNER AND t.TABLE_NAME = c.TABLE_NAME "
            f"WHERE t.OWNER = {_sql_literal(schema_upper)} "
            "ORDER BY t.TABLE_NAME"
        ),
        (
            "SELECT t.TABLE_NAME, c.COMMENTS, NULL AS NUM_ROWS "
            "FROM ALL_TABLES t "
            "LEFT JOIN ALL_TAB_COMMENTS c "
            f"ON t.OWNER = c.OWNER AND t.TABLE_NAME = c.TABLE_NAME "
            f"WHERE t.OWNER = {_sql_literal(schema_upper)} "
            "ORDER BY t.TABLE_NAME"
        ),
    )
    try:
        for sql in sql_variants:
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
                if rows:
                    for row in rows:
                        name = str(row[0])
                        comment = str(row[1]).strip() if row[1] is not None else ""
                        row_est = row[2] if len(row) > 2 else None
                        try:
                            est = int(row_est) if row_est is not None else None
                        except (TypeError, ValueError):
                            est = None
                        items.append(
                            {
                                "table": name,
                                "comment": comment,
                                "row_estimate": est,
                            }
                        )
                        if len(items) >= max_tables:
                            break
                    break
            except Exception as exc:
                logger.debug("list_tables_with_comments fallback: %s", exc)
                continue
        return items
    finally:
        cursor.close()


def describe_table_full(
    conn,
    schema: str,
    table_name: str,
) -> Dict[str, Any]:
    """表结构 + 列注释；失败时 errors 非空。"""
    schema_upper = schema.upper()
    table_upper = table_name.upper()
    errors: List[str] = []
    columns: List[Dict[str, Any]] = []
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE "
            "FROM ALL_TAB_COLUMNS "
            f"WHERE TABLE_NAME = {_sql_literal(table_upper)} "
            f"AND OWNER = {_sql_literal(schema_upper)} "
            "ORDER BY COLUMN_ID"
        )
        col_rows = cursor.fetchall()
        col_comments: Dict[str, str] = {}
        try:
            cursor.execute(
                "SELECT COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS "
                f"WHERE TABLE_NAME = {_sql_literal(table_upper)} "
                f"AND OWNER = {_sql_literal(schema_upper)}"
            )
            for crow in cursor.fetchall():
                col_comments[str(crow[0]).upper()] = (
                    str(crow[1]).strip() if crow[1] is not None else ""
                )
        except Exception as exc:
            errors.append(f"col_comments: {exc}")

        for row in col_rows:
            name = str(row[0])
            columns.append(
                {
                    "name": name,
                    "type": str(row[1]) if row[1] is not None else "",
                    "length": row[2],
                    "nullable": row[3] if len(row) > 3 else None,
                    "comment": col_comments.get(name.upper(), ""),
                }
            )
    except Exception as exc:
        errors.append(str(exc))
    finally:
        cursor.close()

    if not columns:
        cur = conn.cursor()
        try:
            _set_schema(cur, schema_upper)
            col_names, rows = _fetch_sample_rows(
                cur, schema_upper, table_name, limit=1
            )
            if col_names:
                columns = [
                    {
                        "name": c,
                        "type": "",
                        "length": None,
                        "nullable": None,
                        "comment": "",
                    }
                    for c in col_names
                ]
            if not rows and not col_names:
                errors.append("describe and sample both empty")
        except Exception as exc:
            errors.append(f"sample infer: {exc}")
        finally:
            cur.close()

    return {
        "table": table_name,
        "schema": schema_upper,
        "columns": columns,
        "errors": errors,
    }


def execute_readonly_query(
    conn,
    schema: str,
    sql: str,
    *,
    max_rows: int = 50,
) -> Dict[str, Any]:
    """在 schema 下执行已通过 sql_guard 的只读 SELECT。"""
    cursor = conn.cursor()
    try:
        _set_schema(cursor, schema.upper())
        cursor.execute(sql)
        if not cursor.description:
            return {
                "ok": True,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": None,
                "sql_executed": sql,
            }
        col_names = [desc[0] for desc in cursor.description]
        raw_rows = cursor.fetchall()
        rows = [_row_to_dict(col_names, row) for row in raw_rows]
        return {
            "ok": True,
            "columns": col_names,
            "rows": rows,
            "row_count": len(rows),
            "error": None,
            "sql_executed": sql,
        }
    except Exception as exc:
        return {
            "ok": False,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": str(exc),
            "sql_executed": sql,
        }
    finally:
        cursor.close()


def pick_publish_time(row: Dict[str, Any], columns_meta: Sequence[Dict[str, Any]]) -> Optional[datetime]:
    for col in columns_meta:
        name = col.get("name", "")
        if not _is_date_column(name, col.get("type", "")):
            continue
        raw = row.get(name)
        if raw is None:
            continue
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, date):
            return datetime.combine(raw, datetime.min.time())
        text = str(raw).strip()
        if not text:
            continue
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y%m%d",
        ):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                continue
    return None
