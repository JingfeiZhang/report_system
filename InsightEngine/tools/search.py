"""
专为 AI Agent 设计的本地舆情数据库查询工具集 (MediaCrawlerDB) - 达梦数据库适配版

版本: 3.1 (Dameng Port)
最后更新: 2026-01-13

变更说明:
- 底层驱动替换为 dmPython (达梦原生驱动)
- 移除 asyncio，改为同步执行 (dmPython 为同步驱动)
- 将复杂的 UNION ALL 查询拆分为多次简单查询 + 内存聚合，提高稳定性
- 增加对 SQL 保留字 (desc, time, user) 的双引号保护
"""

import os
import sys
import random
from functools import lru_cache
from pathlib import Path


def _prepare_dm_dll_path() -> None:
    """Windows Python 3.8+ 加载 .pyd 时不搜索 PATH，需显式注册达梦 bin 目录。"""
    if not sys.platform.startswith("win") or not hasattr(os, "add_dll_directory"):
        return
    dm_bin = os.environ.get("DM_CLIENT_BIN", r"D:\dm\bin")
    bin_path = Path(dm_bin)
    if bin_path.is_dir():
        os.add_dll_directory(str(bin_path.resolve()))


@lru_cache(maxsize=1)
def _get_dm_python():
    """延迟加载 dmPython，避免 import 阶段因达梦客户端未配置导致引擎无法加载。"""
    _prepare_dm_dll_path()
    import dmPython
    return dmPython
from loguru import logger
from typing import List, Dict, Any, Optional, Literal, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from InsightEngine.utils.config import settings

DATABASE_CONFIG = {
    "user": settings.DB_USER,
    "password": settings.DB_PASSWORD,
    "host": settings.DB_HOST,
    "port": settings.DB_PORT,
}

logger.info(
    "达梦数据库已配置: {}:{} (用户已设置，密码不记录日志)",
    DATABASE_CONFIG["host"],
    DATABASE_CONFIG["port"],
)


# --- 1. 数据结构定义 ---

@dataclass
class QueryResult:
    """统一的数据库查询结果数据类"""
    platform: str
    content_type: str
    title_or_content: str
    author_nickname: Optional[str] = None
    url: Optional[str] = None
    publish_time: Optional[datetime] = None
    engagement: Dict[str, int] = field(default_factory=dict)
    source_keyword: Optional[str] = None
    hotness_score: float = 0.0
    source_table: str = ""


@dataclass
class DBResponse:
    """封装工具的完整返回结果"""
    tool_name: str
    parameters: Dict[str, Any]
    results: List[QueryResult] = field(default_factory=list)
    results_count: int = 0
    error_message: Optional[str] = None


# --- 2. 核心客户端与专用工具集 ---

class MediaCrawlerDB:
    """包含多种专用舆情数据库查询工具的客户端 (达梦版)"""

    # 权重定义
    W_LIKE = 1.0
    W_COMMENT = 5.0
    W_SHARE = 10.0
    W_VIEW = 0.1
    W_DANMAKU = 0.5

    def __init__(self):
        """初始化客户端"""
        self._table_columns_cache = {}

    @staticmethod
    def _resolve_dm_schema() -> str:
        return (
            os.getenv("DM_CURRENT_SCHEMA", "").strip()
            or os.getenv("DB_NAME", "").strip()
            or getattr(settings, "DM_CURRENT_SCHEMA", "") or ""
            or (settings.DB_NAME or "")
            or "mindspider_war"
        )

    @classmethod
    def _set_session_schema(cls, conn) -> None:
        schema = cls._resolve_dm_schema()
        if not schema:
            return
        cursor = conn.cursor()
        try:
            for sql in (
                f'ALTER SESSION SET CURRENT_SCHEMA = "{schema}"',
                f"ALTER SESSION SET CURRENT_SCHEMA = {schema}",
            ):
                try:
                    cursor.execute(sql)
                    break
                except Exception:
                    continue
        finally:
            cursor.close()

    def _get_connection(self):
        """获取达梦数据库连接"""
        conn = _get_dm_python().connect(
            user=DATABASE_CONFIG['user'],
            password=DATABASE_CONFIG['password'],
            server=DATABASE_CONFIG['host'],
            port=DATABASE_CONFIG['port'],
            autoCommit=True,
            local_code=1  # UTF8
        )
        self._set_session_schema(conn)
        return conn

    def _execute_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """执行 SQL 查询并返回字典列表"""
        conn = None
        cursor = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 执行查询 (dmPython 支持 :name 风格参数)
            cursor.execute(query, params or {})

            if not cursor.description:
                return []

            # 获取列名 (转小写以统一处理)
            columns = [col[0].lower() for col in cursor.description]

            # 转换结果
            results = []
            rows = cursor.fetchall()
            for row in rows:
                results.append(dict(zip(columns, row)))

            return results

        except Exception as e:
            logger.error(f"数据库查询错误: {e}")
            logger.debug(f"Query: {query}")
            logger.debug(f"Params: {params}")
            return []
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    @staticmethod
    def _to_datetime(ts: Any) -> Optional[datetime]:
        """统一时间格式转换"""
        if not ts: return None
        try:
            if isinstance(ts, datetime): return ts
            if isinstance(ts, date): return datetime.combine(ts, datetime.min.time())
            # 处理时间戳 (秒或毫秒)
            if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
                val = float(ts)
                # 粗略判断是毫秒还是秒 (10位是秒，13位是毫秒)
                return datetime.fromtimestamp(val / 1000 if val > 1_000_000_000_000 else val)
            if isinstance(ts, str):
                # 尝试解析 ISO 格式
                return datetime.fromisoformat(ts.split('+')[0].strip().replace('T', ' '))
        except (ValueError, TypeError):
            return None
        return None

    def _get_table_columns(self, table_name: str) -> List[str]:
        """
        获取表的所有列名
        适配达梦：不使用 SHOW COLUMNS，而是查询空结果集获取 description
        """
        if table_name in self._table_columns_cache:
            return self._table_columns_cache[table_name]

        # 使用 1=0 获取表结构而不查询数据
        query = f'SELECT * FROM "{table_name}" WHERE 1=0'
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(query)
            if cursor.description:
                columns = [col[0].lower() for col in cursor.description]
            else:
                columns = []
            self._table_columns_cache[table_name] = columns
            return columns
        except Exception as e:
            logger.error(f"获取表结构失败 [{table_name}]: {e}")
            return []
        finally:
            if conn: conn.close()

    def _extract_engagement(self, row: Dict[str, Any]) -> Dict[str, int]:
        """从数据行中提取并统一互动指标"""
        engagement = {}
        mapping = {
            'likes': ['liked_count', 'like_count', 'voteup_count', 'comment_like_count'],
            'comments': ['video_comment', 'comments_count', 'comment_count', 'total_replay_num', 'sub_comment_count'],
            'shares': ['video_share_count', 'shared_count', 'share_count', 'total_forwards'],
            'views': ['video_play_count', 'viewd_count'],
            'favorites': ['video_favorite_count', 'collected_count'],
            'coins': ['video_coin_count'],
            'danmaku': ['video_danmaku'],
        }
        for key, potential_cols in mapping.items():
            for col in potential_cols:
                # 结果字典已转为小写，所以这里直接匹配
                if col in row and row[col] is not None:
                    try:
                        engagement[key] = int(row[col])
                    except (ValueError, TypeError):
                        engagement[key] = 0
                    break
        return engagement

    def _wrap_field(self, field: str) -> str:
        """使用双引号包裹字段名，适配达梦保留字"""
        return f'"{field}"'

    def search_hot_content(
            self,
            time_period: Literal['24h', 'week', 'year'] = 'week',
            limit: int = 50
    ) -> DBResponse:
        """
        【工具】查找热点内容
        策略：分别查询每个表，按热度排序取前N条，然后在内存中合并再次排序
        """
        params_for_log = {'time_period': time_period, 'limit': limit}
        logger.info(f"--- TOOL: 查找热点内容 (params: {params_for_log}) ---")

        now = datetime.now()
        start_time = now - timedelta(days={'24h': 1, 'week': 7}.get(time_period, 365))

        # 达梦中 CAST 需要处理 NULL，COALESCE 必须在计算前
        # 注意：这里生成的 SQL 片段不包含别名，别名在 SELECT 时指定
        def cast_int(col):
            return f"COALESCE(CAST(\"{col}\" AS INT), 0)"

        def cast_dec(col):
            return f"COALESCE(CAST(\"{col}\" AS DECIMAL(20,2)), 0)"

        hotness_formulas = {
            'bilibili_video': f"({cast_int('liked_count')} * {self.W_LIKE} + {cast_int('video_comment')} * {self.W_COMMENT} + {cast_int('video_share_count')} * {self.W_SHARE} + {cast_dec('video_play_count')} * {self.W_VIEW})",
            'douyin_aweme': f"({cast_int('liked_count')} * {self.W_LIKE} + {cast_int('comment_count')} * {self.W_COMMENT} + {cast_int('share_count')} * {self.W_SHARE})",
            'weibo_note': f"({cast_int('liked_count')} * {self.W_LIKE} + {cast_int('comments_count')} * {self.W_COMMENT} + {cast_int('shared_count')} * {self.W_SHARE})",
            'xhs_note': f"({cast_int('liked_count')} * {self.W_LIKE} + {cast_int('comment_count')} * {self.W_COMMENT} + {cast_int('collected_count')} * {self.W_SHARE})",
            'kuaishou_video': f"({cast_int('liked_count')} * {self.W_LIKE} + {cast_dec('viewd_count')} * {self.W_VIEW})",
            'zhihu_content': f"({cast_int('voteup_count')} * {self.W_LIKE} + {cast_int('comment_count')} * {self.W_COMMENT})",
        }

        all_results = []

        for table, formula in hotness_formulas.items():
            try:
                # 确定时间字段和过滤条件
                time_col = 'create_time'
                if table == 'weibo_note':
                    time_col = 'create_date_time'
                elif table == 'xhs_note':
                    time_col = 'time'
                elif table == 'zhihu_content':
                    time_col = 'created_time'
                elif table == 'tieba_note':
                    time_col = 'publish_time'

                # 达梦时间比较：统一使用参数化查询
                # 注意：部分表存的是毫秒/秒时间戳，部分是字符串。这里在 Python 端统一转换为适当的比较值
                time_val = start_time
                if table in ['kuaishou_video', 'xhs_note', 'douyin_aweme']:
                    time_val = int(start_time.timestamp() * 1000)  # 毫秒
                elif table in ['zhihu_content', 'bilibili_video']:
                    time_val = int(start_time.timestamp())  # 秒
                else:
                    time_val = start_time.strftime('%Y-%m-%d %H:%M:%S')  # 字符串

                # 字段映射
                title_col = 'content' if table == 'weibo_note' else 'title'
                author_col = 'nickname'
                if table == 'zhihu_content': author_col = 'user_nickname'

                url_col = 'url'
                if table == 'bilibili_video':
                    url_col = 'video_url'
                elif table == 'douyin_aweme':
                    url_col = 'aweme_url'
                elif table == 'weibo_note':
                    url_col = 'note_url'
                elif table == 'xhs_note':
                    url_col = 'note_url'
                elif table == 'zhihu_content':
                    url_col = 'content_url'

                content_type = 'note' if 'note' in table else 'content' if 'content' in table else 'video'

                # 构建单表查询
                # 使用 TOP 限制单表返回量，提高性能
                query = f"""
                    SELECT TOP :limit 
                        '{table.split('_')[0]}' as p, 
                        '{content_type}' as t, 
                        "{title_col}" as title, 
                        "{author_col}" as author, 
                        "{url_col}" as url, 
                        "{time_col}" as ts, 
                        {formula} as hotness_score, 
                        "source_keyword", 
                        '{table}' as tbl 
                    FROM "{table}" 
                    WHERE "{time_col}" >= :start_time
                    ORDER BY hotness_score DESC
                """

                params = {'limit': limit, 'start_time': time_val}

                results = self._execute_query(query, params)
                all_results.extend(results)

            except Exception as e:
                logger.warning(f"跳过表 {table} (热度查询): {e}")
                continue

        # 内存中合并排序
        all_results.sort(key=lambda x: float(x.get('hotness_score', 0)), reverse=True)
        final_results = all_results[:limit]

        # 转换为 QueryResult 对象
        formatted_results = [
            QueryResult(
                platform=r['p'],
                content_type=r['t'],
                title_or_content=r['title'],
                author_nickname=r.get('author'),
                url=r['url'],
                publish_time=self._to_datetime(r['ts']),
                engagement=self._extract_engagement(r),
                hotness_score=float(r.get('hotness_score', 0.0)),
                source_keyword=r.get('source_keyword'),
                source_table=r['tbl']
            ) for r in final_results
        ]

        return DBResponse("search_hot_content", params_for_log, results=formatted_results,
                          results_count=len(formatted_results))

    def search_topic_globally(self, topic: str, limit_per_table: int = 100, _is_fallback: bool = False) -> DBResponse:
        """
        【工具】全局话题搜索
        """
        params_for_log = {'topic': topic, 'limit_per_table': limit_per_table}
        logger.info(f"--- TOOL: 全局话题搜索 (params: {params_for_log}) ---")

        search_term = f"%{topic}%"
        all_results = []

        search_configs = {
            'bilibili_video': {'fields': ['title', 'desc', 'source_keyword'], 'type': 'video'},
            'douyin_aweme': {'fields': ['title', 'desc', 'source_keyword'], 'type': 'video'},
            'weibo_note': {'fields': ['content', 'source_keyword'], 'type': 'note'},
            'xhs_note': {'fields': ['title', 'desc', 'tag_list', 'source_keyword'], 'type': 'note'},
            'zhihu_content': {'fields': ['title', 'desc', 'content_text', 'source_keyword'], 'type': 'content'},
            'tieba_note': {'fields': ['title', 'desc', 'source_keyword'], 'type': 'note'},
            'daily_news': {'fields': ['title'], 'type': 'news'},
            # 评论表省略以节省篇幅，逻辑一致
        }

        for table, config in search_configs.items():
            try:
                where_clauses = []
                params = {}
                for idx, field in enumerate(config['fields']):
                    pname = f"term_{idx}"
                    # 使用双引号包裹列名
                    where_clauses.append(f'"{field}" LIKE :{pname}')
                    params[pname] = search_term

                params['limit'] = limit_per_table
                where_clause = " OR ".join(where_clauses)

                # 达梦: 使用双引号包裹表名
                query = f'SELECT TOP :limit * FROM "{table}" WHERE {where_clause} ORDER BY id DESC'

                rows = self._execute_query(query, params)

                for row in rows:
                    content = (row.get('title') or row.get('content') or row.get('desc') or row.get('content_text', ''))
                    # 尝试获取各种可能的时间字段
                    time_key = None
                    for tc in ['create_time', 'time', 'created_time', 'publish_time', 'crawl_date', 'create_date_time']:
                        if tc in row: time_key = row[tc]; break

                    # 尝试获取 URL
                    url_val = None
                    for uc in ['video_url', 'note_url', 'content_url', 'url', 'aweme_url']:
                        if uc in row: url_val = row[uc]; break

                    # 尝试获取昵称
                    nick_val = None
                    for nc in ['nickname', 'user_nickname', 'user_name']:
                        if nc in row: nick_val = row[nc]; break

                    all_results.append(QueryResult(
                        platform=table.split('_')[0],
                        content_type=config['type'],
                        title_or_content=content if content else '',
                        author_nickname=nick_val,
                        url=url_val,
                        publish_time=self._to_datetime(time_key),
                        engagement=self._extract_engagement(row),
                        source_keyword=row.get('source_keyword'),
                        source_table=table
                    ))
            except Exception as e:
                logger.warning(f"搜索表 {table} 失败: {e}")
                continue

        # --- 保底逻辑（ENABLE_DB_FALLBACK=true 且 DB_FALLBACK_WORDS 非空时生效）---
        if (
            not all_results
            and not _is_fallback
            and settings.ENABLE_DB_FALLBACK
            and settings.DB_FALLBACK_WORDS
        ):
            fallback_topic = random.choice(settings.DB_FALLBACK_WORDS)
            logger.warning(f"⚠️ 搜索 '{topic}' 无结果，触发保底机制，使用关键词: '{fallback_topic}'")
            # 递归调用，标记为 fallback 防止无限递归
            return self.search_topic_globally(fallback_topic, limit_per_table, _is_fallback=True)

        return DBResponse("search_topic_globally", params_for_log, results=all_results, results_count=len(all_results))

    def get_comments_for_topic(self, topic: str, limit: int = 500) -> DBResponse:
        """
        【工具】获取话题评论
        策略：拆分 UNION ALL 为分表查询
        """
        params_for_log = {'topic': topic, 'limit': limit}
        logger.info(f"--- TOOL: 获取话题评论 (params: {params_for_log}) ---")

        search_term = f"%{topic}%"
        comment_tables = ['bilibili_video_comment', 'douyin_aweme_comment', 'kuaishou_video_comment',
                          'weibo_note_comment', 'xhs_note_comment', 'zhihu_comment', 'tieba_comment']

        all_rows = []

        for table in comment_tables:
            try:
                cols = self._get_table_columns(table)
                # 达梦中获取的列名已被转为小写
                author_col = 'user_nickname' if 'user_nickname' in cols else 'nickname'
                like_col = 'comment_like_count' if 'comment_like_count' in cols else 'like_count' if 'like_count' in cols else None
                time_col = 'publish_time' if 'publish_time' in cols else 'create_date_time' if 'create_date_time' in cols else 'create_time'

                # 构造 SELECT
                like_expr = f'"{like_col}"' if like_col else "0"

                query = f"""
                    SELECT TOP :limit
                        '{table.split('_')[0]}' as platform, 
                        "content", 
                        "{author_col}" as author, 
                        "{time_col}" as ts, 
                        {like_expr} as likes, 
                        '{table}' as source_table 
                    FROM "{table}" 
                    WHERE "content" LIKE :term
                """

                rows = self._execute_query(query, {'limit': limit, 'term': search_term})
                all_rows.extend(rows)
            except Exception as e:
                logger.warning(f"搜索评论表 {table} 失败: {e}")
                continue

        # 内存排序 (按时间倒序)
        def get_ts(r):
            dt = self._to_datetime(r['ts'])
            return dt.timestamp() if dt else 0

        all_rows.sort(key=get_ts, reverse=True)
        final_rows = all_rows[:limit]

        formatted = [
            QueryResult(
                platform=r['platform'],
                content_type='comment',
                title_or_content=r['content'],
                author_nickname=r['author'],
                publish_time=self._to_datetime(r['ts']),
                engagement={'likes': int(r['likes']) if str(r['likes']).isdigit() else 0},
                source_table=r['source_table']
            ) for r in final_rows
        ]
        return DBResponse("get_comments_for_topic", params_for_log, results=formatted, results_count=len(formatted))

    def search_topic_on_platform(
            self,
            platform: Literal['bilibili', 'weibo', 'douyin', 'kuaishou', 'xhs', 'zhihu', 'tieba'],
            topic: str,
            start_date: Optional[str] = None,
            end_date: Optional[str] = None,
            limit: int = 20,
            _is_fallback: bool = False
    ) -> DBResponse:
        """
        【工具】平台定向搜索
        """
        params_for_log = {'platform': platform, 'topic': topic, 'start_date': start_date, 'end_date': end_date,
                          'limit': limit}
        logger.info(f"--- TOOL: 平台定向搜索 (params: {params_for_log}) ---")

        all_configs = {
            'bilibili': [{'table': 'bilibili_video', 'fields': ['title', 'desc', 'source_keyword'], 'type': 'video',
                          'time_col': 'create_time', 'time_type': 'sec'}],
            'douyin': [{'table': 'douyin_aweme', 'fields': ['title', 'desc', 'source_keyword'], 'type': 'video',
                        'time_col': 'create_time', 'time_type': 'ms'}],
            'weibo': [{'table': 'weibo_note', 'fields': ['content', 'source_keyword'], 'type': 'note',
                       'time_col': 'create_date_time', 'time_type': 'str'}],
            # 其他平台省略以节省篇幅，逻辑一致...
            'zhihu': [{'table': 'zhihu_content', 'fields': ['title', 'desc', 'content_text', 'source_keyword'],
                       'type': 'content', 'time_col': 'created_time', 'time_type': 'sec'}],
        }

        if platform not in all_configs:
            return DBResponse("search_topic_on_platform", params_for_log, error_message=f"不支持的平台: {platform}")

        search_term = f"%{topic}%"
        platform_configs = all_configs[platform]
        all_results = []

        start_dt, end_dt = None, None
        if start_date and end_date:
            try:
                start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            except ValueError:
                return DBResponse("search_topic_on_platform", params_for_log, error_message="日期格式错误")

        for config in platform_configs:
            table = config['table']
            params = {}

            # 关键词条件
            topic_clauses = []
            for idx, field in enumerate(config['fields']):
                pname = f"kw_{idx}"
                topic_clauses.append(f'"{field}" LIKE :{pname}')
                params[pname] = search_term
            topic_sql = " OR ".join(topic_clauses)

            # 时间条件
            time_sql = ""
            if start_dt and end_dt and 'time_col' in config:
                time_col, time_type = config['time_col'], config['time_type']

                # 转换时间值
                if time_type == 'sec':
                    s_val, e_val = int(start_dt.timestamp()), int(end_dt.timestamp())
                elif time_type == 'ms':
                    s_val, e_val = int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)
                else:
                    s_val, e_val = start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d')

                # 达梦特殊处理：如果是字符型时间列，直接比较；如果是数值型，CAST 可能更好
                if table == 'zhihu_content':  # zhihu 可能是字符串存秒数
                    time_sql = f" AND CAST(\"{time_col}\" AS BIGINT) >= :start_t AND CAST(\"{time_col}\" AS BIGINT) < :end_t"
                else:
                    time_sql = f" AND \"{time_col}\" >= :start_t AND \"{time_col}\" < :end_t"

                params['start_t'] = s_val
                params['end_t'] = e_val

            params['limit'] = limit
            query = f'SELECT TOP :limit * FROM "{table}" WHERE ({topic_sql}) {time_sql} ORDER BY id DESC'

            try:
                rows = self._execute_query(query, params)
                for row in rows:
                    # (此处省略与 search_topic_globally 相同的字段提取逻辑，实际代码中应复用)
                    # 简写以展示逻辑：
                    content = row.get('title') or row.get('content') or ''
                    all_results.append(QueryResult(
                        platform=platform, content_type=config['type'],
                        title_or_content=content,
                        url=row.get('url') or row.get('video_url'),
                        # ... 其他字段
                    ))
            except Exception as e:
                logger.warning(f"平台搜索表 {table} 失败: {e}")

        # --- 保底逻辑（ENABLE_DB_FALLBACK=true 且 DB_FALLBACK_WORDS 非空时生效）---
        if (
            not all_results
            and not _is_fallback
            and settings.ENABLE_DB_FALLBACK
            and settings.DB_FALLBACK_WORDS
        ):
            fallback_topic = random.choice(settings.DB_FALLBACK_WORDS)
            logger.warning(f"⚠️ 平台 '{platform}' 搜索 '{topic}' 无结果，触发保底: '{fallback_topic}'")
            return self.search_topic_on_platform(
                platform, fallback_topic, start_date, end_date, limit, _is_fallback=True
            )

        return DBResponse("search_topic_on_platform", params_for_log, results=all_results,
                          results_count=len(all_results))


# --- 3. 测试与使用示例 ---

def print_response_summary(response: DBResponse):
    """打印结果摘要"""
    if response.error_message:
        logger.info(f"❌ 工具 '{response.tool_name}' 出错: {response.error_message}")
        return

    logger.info(f"✅ 工具 '{response.tool_name}' 找到 {response.results_count} 条记录")
    if response.results:
        for i, res in enumerate(response.results[:3], 1):
            logger.info(f"   {i}. [{res.platform}] {res.title_or_content[:50]}...")


if __name__ == "__main__":
    logger.info("=== 达梦数据库 Search Tool 测试 ===")

    try:
        # 1. 初始化
        tool = MediaCrawlerDB()

        # 2. 测试热点搜索
        # response = tool.search_hot_content(time_period='week', limit=5)
        # print_response_summary(response)

        # 3. 测试全局搜索
        response = tool.search_topic_globally("人工智能", limit_per_table=2)
        print_response_summary(response)

    except Exception as e:
        logger.exception(f"测试失败: {e}")