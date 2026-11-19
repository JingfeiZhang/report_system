"""TROOPS Agent LLM 提示词。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence


SELECT_TABLES_SYSTEM = """你是达梦军事业务库（TROOPS schema）的表路由助手。
根据用户讨论主题，从「表名 + 表注释」目录中选出最相关的 1~3 张业务表。
禁止选择：AGENTS、AGENT_SESSION、## 开头、明显系统/统计/会话表。
只输出 JSON，不要 markdown 围栏。"""


PLAN_SQL_SYSTEM = """你是达梦库只读 SQL 助手。根据用户主题与表结构，编写一条 SELECT 查询。
要求：
1. 仅 SELECT，不要 INSERT/UPDATE/DELETE
2. 尽量用与主题相关的 WHERE（中文 LIKE 可用）
3. 必须包含 ROWNUM 限制（建议 <= 30）
4. 只查必要列，不要 SELECT *
5. 只输出 JSON，不要 markdown 围栏。"""


def build_select_tables_user_prompt(topic: str, catalog: Sequence[Dict[str, Any]]) -> str:
    lines = [f"讨论主题：{topic.strip()}", "", "表目录（表名 | 注释 | 估计行数）："]
    for item in catalog[:120]:
        name = item.get("table") or ""
        comment = (item.get("comment") or "").strip() or "（无注释）"
        est = item.get("row_estimate")
        est_s = str(est) if est is not None else "?"
        lines.append(f"- {name} | {comment} | rows~{est_s}")
    lines.append("")
    lines.append(
        '输出格式：{"tables":["表名1","表名2"],"reason":"…","confidence":"high|medium|low"}'
    )
    return "\n".join(lines)


def build_plan_sql_user_prompt(
    topic: str,
    table: str,
    describe: Dict[str, Any],
) -> str:
    cols = describe.get("columns") or []
    col_lines = []
    for c in cols[:40]:
        cn = c.get("comment") or ""
        extra = f" /*{cn}*/" if cn else ""
        col_lines.append(f"  - {c.get('name')} {c.get('type')}{extra}")
    body = "\n".join(col_lines) if col_lines else "  （无列信息）"
    return (
        f"讨论主题：{topic.strip()}\n"
        f"目标表：{table}\n"
        f"列信息：\n{body}\n\n"
        '输出格式：{"queries":[{"table":"表名","sql":"SELECT ...","purpose":"…"}]}'
    )


def build_sql_fix_user_prompt(
    topic: str,
    table: str,
    bad_sql: str,
    error: str,
) -> str:
    return (
        f"讨论主题：{topic}\n表：{table}\n"
        f"失败 SQL：{bad_sql}\n错误：{error}\n"
        "请修正为可执行的只读 SELECT（含 ROWNUM）。\n"
        '输出格式：{"queries":[{"table":"…","sql":"…","purpose":"…"}]}'
    )


def format_catalog_json(catalog: Sequence[Dict[str, Any]], limit: int = 120) -> str:
    return json.dumps(list(catalog)[:limit], ensure_ascii=False, indent=0)
