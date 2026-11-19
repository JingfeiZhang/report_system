"""
多库达梦数据库 MCP 工具（HTTP SSE 流式传输）
=============================================
功能：
  - 表内容分析：遍历多个 schema，采集所有表的结构、采样数据、行数统计
  - SQL 查询：在指定 schema 中执行 SELECT/SHOW/DESCRIBE/EXPLAIN
  - 表结构查看：查看指定表的列信息
  - 结果以结构化 JSON 返回，由大模型自行分析分类

数据库连接信息通过环境变量配置（参考 server.py 风格）：
  DM_HOST, DM_PORT, DM_USERNAME, DM_PASSWORD
  DM_SCHEMA_LIST: 逗号分隔的模式名列表，如 "SCHEMA_A,SCHEMA_B"

启动方式：
  python server_multi_db.py          # SSE 监听 0.0.0.0:8002
"""

import os
import sys

# ---------------------------------------------------------------------------
# LD_LIBRARY_PATH 设置（必须在 import dmPython 之前）
# 与 config.py 中的逻辑一致，确保 dmPython 的加密模块可加载
# ---------------------------------------------------------------------------
_DMSSL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", ".venv", "lib", "python3.11", "site-packages", "dmssl",
)
_DMSSL_PATH = os.path.normpath(_DMSSL_PATH)

if "LD_LIBRARY_PATH" not in os.environ or _DMSSL_PATH not in os.environ.get("LD_LIBRARY_PATH", ""):
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = _DMSSL_PATH + (":" + existing if existing else "")
    if sys.argv[0] and sys.executable:
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f"Failed to restart with LD_LIBRARY_PATH={_DMSSL_PATH}: {e}", file=sys.stderr)

import json
import logging
import os
import sys

_PKG_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

import dmPython
from fastmcp import FastMCP

from mcp_server_dm.schema_search import (
    discover_tables,
    load_schema_search_config,
    resolve_dm_connect_kwargs,
    search_schema_globally,
)

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mcp_dameng_multi_db")

# ---------------------------------------------------------------------------
# FastMCP 实例
# ---------------------------------------------------------------------------
mcp = FastMCP("dameng-multi-db")


# ===================================================================
#  数据库连接（从环境变量读取，与 server.py 风格一致）
# ===================================================================

def get_connection():
    """从环境变量读取连接信息并返回数据库连接。"""
    kwargs = resolve_dm_connect_kwargs()
    logger.info(
        "Connecting to DM database at %(server)s:%(port)s as %(user)s",
        kwargs,
    )
    return dmPython.connect(**kwargs)


def get_schema_list() -> list[str]:
    """从环境变量 DM_SCHEMA_LIST 获取逗号分隔的模式名列表。"""
    raw = os.environ.get("DM_SCHEMA_LIST", "")
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# ===================================================================
#  数据库操作
# ===================================================================

def _discover_tables(conn, schema: str) -> list[tuple[str, str]]:
    """发现指定 schema 下的所有用户表（排除视图）。"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT TABLE_NAME, OWNER FROM ALL_TABLES "
            f"WHERE OWNER = '{schema}' AND TABLE_TYPE = 'TABLE' "
            "ORDER BY TABLE_NAME"
        )
        rows = cursor.fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]
    except Exception:
        # 降级：无 TABLE_TYPE 过滤
        cursor.execute(
            f"SELECT TABLE_NAME, OWNER FROM ALL_TABLES WHERE OWNER = '{schema}'"
        )
        rows = cursor.fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]
    finally:
        cursor.close()


def _get_columns(conn, table_name: str, schema: str) -> list[dict]:
    """获取表的列信息。"""
    cursor = conn.cursor()
    try:
        sql = (
            f"SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, "
            f"DATA_SCALE, NULLABLE "
            f"FROM ALL_TAB_COLUMNS "
            f"WHERE TABLE_NAME = '{table_name.upper()}' AND OWNER = '{schema}' "
            "ORDER BY COLUMN_ID"
        )
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = []
        for row in rows:
            columns.append({
                "name": row[0],
                "type": row[1],
                "length": row[2] if len(row) > 2 else None,
                "precision": row[3] if len(row) > 3 else None,
                "scale": row[4] if len(row) > 4 else None,
                "nullable": row[5] if len(row) > 5 else None,
            })
        return columns
    except Exception:
        sql = (
            f"SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE "
            f"FROM ALL_TAB_COLUMNS "
            f"WHERE TABLE_NAME = '{table_name.upper()}' AND OWNER = '{schema}'"
        )
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [
            {"name": r[0], "type": r[1], "length": r[2] if len(r) > 2 else None,
             "precision": None, "scale": None, "nullable": r[3] if len(r) > 3 else None}
            for r in rows
        ]
    finally:
        cursor.close()


def _sample_rows(conn, table_name: str, schema: str, limit: int = 10) -> list[dict]:
    """采样表的前 N 行数据。"""
    cursor = conn.cursor()
    try:
        cursor.execute(f"SET SCHEMA {schema}")
        cursor.execute(f"SELECT * FROM {table_name} WHERE ROWNUM <= {limit}")
        rows = cursor.fetchall()
        if not rows:
            return []
        col_names = [desc[0] for desc in cursor.description]
        result = []
        for row in rows:
            record = {}
            for i, col_name in enumerate(col_names):
                val = row[i]
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                record[col_name] = val
            result.append(record)
        return result
    except Exception as e:
        logger.warning(f"  Sample {schema}.{table_name} failed: {e}")
        return []
    finally:
        cursor.close()


def _get_table_count(conn, table_name: str, schema: str) -> int:
    """获取表的总行数。"""
    cursor = conn.cursor()
    try:
        cursor.execute(f"SET SCHEMA {schema}")
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row = cursor.fetchone()
        return int(row[0]) if row else -1
    except Exception as e:
        logger.warning(f"  Count {schema}.{table_name} failed: {e}")
        return -1
    finally:
        cursor.close()


# ===================================================================
#  MCP 工 具 — SQL 执行（参考 server.py 风格）
# ===================================================================

@mcp.tool()
def list_tables(schema: str) -> str:
    """
    列出指定 schema 下的所有表。

    参数:
        schema: 模式名（必选）
    """
    logger.info(f"Listing tables in schema {schema}")
    conn = None
    try:
        conn = get_connection()
        tables = discover_tables(conn, schema.upper())
        return "\n".join(tables) if tables else "No tables found."
    except Exception as e:
        logger.error(f"Error listing tables: {e}")
        return f"Error: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@mcp.tool()
def execute_query(query: str, schema: str) -> str:
    """
    在指定 schema 中执行只读 SQL 查询（SELECT, SHOW, DESCRIBE, EXPLAIN）。

    参数:
        query:  SQL 语句
        schema: 目标模式名
    """
    logger.info(f"Executing query in schema {schema}: {query}")

    query_upper = query.strip().upper()
    allowed_starts = ["SELECT", "SHOW", "DESCRIBE", "EXPLAIN"]
    if not any(query_upper.startswith(start) for start in allowed_starts):
        return "Error: Only read-only queries (SELECT, SHOW, DESCRIBE, EXPLAIN) are allowed."

    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(f"SET SCHEMA {schema.upper()}")
        cursor.execute(query)

        if cursor.description:
            columns = [desc[0] for desc in cursor.description]
            results = cursor.fetchall()
            output = [f"Columns: {', '.join(columns)}"]
            for row in results:
                output.append(str(row))
            return "\n".join(output)
        else:
            return "Query executed successfully (no results)."
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return f"Error: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@mcp.tool()
def describe_table(table_name: str, schema: str) -> str:
    """
    查看指定 schema 下表的结构（列名、类型、长度、是否可空）。

    参数:
        table_name: 表名
        schema:     目标模式名
    """
    logger.info(f"Describing table {schema}.{table_name}")
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(f"SET SCHEMA {schema.upper()}")

        sql = (
            f"SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE "
            f"FROM ALL_TAB_COLUMNS "
            f"WHERE TABLE_NAME = '{table_name.upper()}' AND OWNER = '{schema.upper()}'"
        )
        cursor.execute(sql)
        columns = cursor.fetchall()

        if not columns:
            return f"Table '{schema}.{table_name}' not found or has no columns."

        output = [f"Table: {schema}.{table_name}", "Column | Type | Length | Nullable", "-" * 50]
        for col in columns:
            output.append(f"{col[0]} | {col[1]} | {col[2]} | {col[3]}")
        return "\n".join(output)
    except Exception as e:
        logger.error(f"Error describing table: {e}")
        return f"Error: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


@mcp.tool()
def search_schema_globally_tool(keyword: str, schema: str) -> str:
    """
    在指定 schema 下遍历所有表，对文本列做 LIKE 检索（只读）。

    参数:
        keyword: 检索关键词
        schema:  模式名，例如 TROOPS_2_ENHANCE
    """
    logger.info("search_schema_globally schema=%s keyword=%s", schema, keyword)
    cfg = load_schema_search_config(schema)
    result = search_schema_globally(keyword, config=cfg)
    return json.dumps(result, ensure_ascii=False, indent=2)


# ===================================================================
#  启 动 入 口（SSE 传输，端口 8002）
# ===================================================================
if __name__ == "__main__":
    port = int(os.environ.get("MULTI_DB_PORT", "8002"))
    host = os.environ.get("MULTI_DB_HOST", "0.0.0.0")
    logger.info("Starting multi-db MCP server (SSE) @ %s:%s", host, port)
    logger.info("Configured schemas: %s", get_schema_list())
    logger.info(
        "Tools: list_tables, execute_query, describe_table, search_schema_globally_tool"
    )
    mcp.run(transport="sse", host=host, port=port)
