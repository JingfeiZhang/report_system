"""只读 SQL 校验与 ROWNUM 限量（TROOPS Agent）。"""

from __future__ import annotations

import re
from typing import Tuple

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|MERGE|CALL|EXEC|EXECUTE)\b",
    re.I,
)
_HAS_ROW_LIMIT = re.compile(
    r"\b(ROWNUM|FETCH\s+FIRST|LIMIT\s+\d+|TOP\s+\d+)\b",
    re.I,
)


def validate_readonly_sql(sql: str) -> Tuple[bool, str]:
    text = (sql or "").strip()
    if not text:
        return False, "SQL 为空"
    if ";" in text.rstrip(";"):
        return False, "不允许多语句"
    if "--" in text or "/*" in text:
        return False, "不允许 SQL 注释"
    upper = text.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, "仅允许 SELECT / WITH ... SELECT"
    if _FORBIDDEN.search(text):
        return False, "检测到非只读关键字"
    return True, ""


def apply_row_limit(sql: str, max_rows: int) -> str:
    """若无行数限制，外包子查询 + ROWNUM。"""
    text = (sql or "").strip().rstrip(";")
    if _HAS_ROW_LIMIT.search(text):
        return text
    limit = max(1, int(max_rows))
    return f"SELECT * FROM ({text}) WHERE ROWNUM <= {limit}"


def guard_sql(sql: str, *, max_rows: int = 50) -> Tuple[bool, str, str]:
    """返回 (ok, error_message, sql_to_execute)。"""
    ok, err = validate_readonly_sql(sql)
    if not ok:
        return False, err, ""
    safe = apply_row_limit(sql, max_rows)
    return True, "", safe
