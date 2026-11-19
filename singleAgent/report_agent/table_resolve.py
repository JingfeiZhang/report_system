"""将 Planner 的表类型提示解析为 TROOPS 真实表名。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence


def resolve_tables_from_hints(
    hints: Sequence[str],
    catalog: Sequence[Dict[str, Any]],
    *,
    max_tables: int = 5,
) -> List[str]:
    """按表注释/表名模糊匹配 hints → 真实表名（保持命中顺序）。"""
    if not hints or not catalog:
        return []
    picked: List[str] = []
    seen: set[str] = set()
    for hint in hints:
        h = (hint or "").strip()
        if not h:
            continue
        h_l = h.lower()
        scored: List[tuple[int, str]] = []
        for item in catalog:
            name = str(item.get("table") or "").strip()
            if not name:
                continue
            comment = str(item.get("comment") or "")
            blob = f"{name} {comment}"
            score = 0
            if h.upper() == name.upper():
                score = 100
            elif h in comment or h in name:
                score = 50
            elif h_l in blob.lower():
                score = 30
            else:
                # 逐字重叠（中文短词）
                for ch in h:
                    if ch.strip() and ch in comment:
                        score += 1
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for _, name in scored:
            key = name.upper()
            if key in seen:
                continue
            seen.add(key)
            picked.append(name)
            if len(picked) >= max_tables:
                return picked
    return picked


@contextmanager
def temporary_table_allowlist(tables: Sequence[str]) -> Iterator[None]:
    """临时覆盖 DM_TROOPS_TABLE_ALLOWLIST（供单次 Troops 检索）。"""
    key = "DM_TROOPS_TABLE_ALLOWLIST"
    legacy_key = "DM_MCP_TABLE_ALLOWLIST"
    old = os.environ.get(key)
    old_legacy = os.environ.get(legacy_key)
    cleaned = [t.strip() for t in tables if t and str(t).strip()]
    try:
        if cleaned:
            joined = ",".join(cleaned)
            os.environ[key] = joined
            # 避免旧白名单干扰
            if legacy_key in os.environ:
                os.environ.pop(legacy_key, None)
        yield
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
        if old_legacy is None:
            os.environ.pop(legacy_key, None)
        else:
            os.environ[legacy_key] = old_legacy


def load_business_catalog_safe() -> List[Dict[str, Any]]:
    """尽力拉取业务表目录；失败返回空列表。"""
    try:
        from singleAgent.troops_agent.tools import (
            filter_business_catalog,
            open_dm_connection,
        )
        from mcp_server_dm.schema_search import (
            list_tables_with_comments,
            load_schema_search_config,
        )

        cfg = load_schema_search_config()
        conn = open_dm_connection()
        try:
            raw = list_tables_with_comments(conn, cfg.schema)
            return filter_business_catalog(raw, config=cfg)
        finally:
            conn.close()
    except Exception:
        return []
