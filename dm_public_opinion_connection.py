#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
舆情事件模块 - 达梦数据库连接示例

来源于项目中的真实连接方式：
- InsightEngine/tools/search.py
- mock_search_api/services/database_service.py

依赖：
    pip install dmPython

环境变量（可按需覆盖）：
    DB_HOST=127.0.0.1
    DB_PORT=5236
    DB_USER=SYSDBA
    DB_PASSWORD=123Abc!@#
    DM_CURRENT_SCHEMA=mindspider_war
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.platform.startswith("win") and hasattr(os, "add_dll_directory"):
    _dm_bin = Path(os.environ.get("DM_CLIENT_BIN", r"D:\dm\bin"))
    if _dm_bin.is_dir():
        os.add_dll_directory(str(_dm_bin.resolve()))

import dmPython


def get_dm_config() -> Dict[str, Any]:
    """读取达梦连接配置。"""
    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "5236")),
        "user": os.getenv("DB_USER", "SYSDBA"),
        "password": os.getenv("DB_PASSWORD", "123Abc!@#"),
        "schema": os.getenv("DM_CURRENT_SCHEMA", "mindspider_war"),
    }


def get_connection(config: Dict[str, Any]):
    """
    按项目现网方式连接达梦：
    - 使用 dmPython 原生驱动
    - 使用 server/port，不传 database
    - local_code=1（UTF8）
    """
    return dmPython.connect(
        user=config["user"],
        password=config["password"],
        server=config["host"],
        port=config["port"],
        autoCommit=True,
        local_code=1,
    )


def switch_schema(cursor, schema: str) -> None:
    """
    切换 schema（与项目中一致，兼容两种写法）。
    """
    for sql in (
        f'ALTER SESSION SET CURRENT_SCHEMA = "{schema}"',
        f"ALTER SESSION SET CURRENT_SCHEMA = {schema}",
    ):
        try:
            cursor.execute(sql)
            return
        except Exception:
            continue


def execute_query(sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """执行查询并返回 list[dict]。"""
    config = get_dm_config()
    conn = None
    cursor = None
    try:
        conn = get_connection(config)
        cursor = conn.cursor()
        switch_schema(cursor, config["schema"])
        cursor.execute(sql, params or {})

        if not cursor.description:
            return []

        columns = [item[0].lower() for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def demo_public_opinion_query() -> None:
    """
    示例：查询舆情数据（weibo_note），返回最近 5 条。
    """
    sql = """
    SELECT TOP 5
        "note_id" AS id,
        "content" AS title_or_content,
        "note_url" AS url,
        "create_time" AS published_ts
    FROM "weibo_note"
    WHERE "content" LIKE :kw
    ORDER BY "create_time" DESC
    """
    rows = execute_query(sql, {"kw": "%舆情%"})
    print(f"命中 {len(rows)} 条数据")
    for idx, item in enumerate(rows, 1):
        print(f"{idx}. {item.get('title_or_content', '')[:60]}")
        print(f"   URL: {item.get('url')}")
        print(f"   时间: {item.get('published_ts')}")


if __name__ == "__main__":
    try:
        demo_public_opinion_query()
    except Exception as exc:
        print(f"执行失败: {exc}")
