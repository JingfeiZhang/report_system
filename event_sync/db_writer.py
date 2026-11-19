#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将按日分组的事件标题合并写入 daily_topics（MySQL / 达梦）。"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING
from urllib.parse import quote_plus

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger("event_sync.db")


def _merge_titles(old: List[str], new: List[str]) -> List[str]:
    """合并去重，保留旧顺序，新词追加在后。"""
    seen = set()
    merged: List[str] = []
    for t in old + new:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        merged.append(t)
    return merged


class DailyTopicsWriter:
    def __init__(self, config: Dict[str, str]):
        self.config = config
        self.dialect = (config.get("DB_DIALECT") or "mysql").lower()
        self.engine: Optional["Engine"] = None
        self.dm_conn = None
        self.is_dm = False
        self._connect()

    def _connect(self) -> None:
        if self.dialect == "dm":
            ms_root = Path(__file__).resolve().parent.parent / "MindSpider"
            if str(ms_root) not in sys.path:
                sys.path.insert(0, str(ms_root))
            from utils.dm_connection import DMConnection

            self.dm_conn = DMConnection(
                {
                    "host": self.config["DB_HOST"],
                    "port": int(self.config.get("DB_PORT") or 5236),
                    "user": self.config["DB_USER"],
                    "password": self.config["DB_PASSWORD"],
                    "database": self.config["DB_NAME"],
                }
            )
            if not self.dm_conn.connect():
                raise RuntimeError("达梦数据库连接失败")
            self.is_dm = True
            logger.info(
                "已连接到达梦: %s@%s:%s/%s",
                self.config["DB_USER"],
                self.config["DB_HOST"],
                self.config.get("DB_PORT", "5236"),
                self.config["DB_NAME"],
            )
            return

        try:
            from sqlalchemy import create_engine
        except ImportError as e:
            raise ImportError(
                "MySQL 写库需要 sqlalchemy 与 PyMySQL，请执行: "
                "pip install -r event_sync/requirements.txt"
            ) from e

        user = self.config["DB_USER"]
        password = quote_plus(self.config["DB_PASSWORD"])
        host = self.config["DB_HOST"]
        port = self.config.get("DB_PORT", "3306")
        name = self.config["DB_NAME"]
        url = (
            f"mysql+pymysql://{user}:{password}@{host}:{port}/{name}"
            f"?charset=utf8mb4"
        )
        self.engine = create_engine(url, future=True)
        self.is_dm = False
        logger.info("已连接到 MySQL: %s@%s:%s/%s", user, host, port, name)

    def close(self) -> None:
        if self.dm_conn:
            self.dm_conn.close()
            self.dm_conn = None
        if self.engine:
            self.engine.dispose()
            self.engine = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def upsert_grouped_titles(self, grouped: Dict[str, List[str]]) -> List[str]:
        """
        grouped: {"YYYY-MM-DD": ["title1", ...]}
        返回成功写入/更新的日期列表（字符串）。
        """
        if not grouped:
            return []

        if self.is_dm:
            return self._upsert_dm(grouped)
        return self._upsert_mysql(grouped)

    def _upsert_mysql(self, grouped: Dict[str, List[str]]) -> List[str]:
        from sqlalchemy import text

        ok_dates: List[str] = []
        assert self.engine is not None

        with self.engine.begin() as conn:
            for date_str, titles in sorted(grouped.items()):
                new_titles = [t.strip() for t in titles if t and t.strip()]
                if not new_titles:
                    continue

                extract_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                topic_id = f"summary_{extract_date.strftime('%Y%m%d')}"
                topic_name = f"{extract_date.strftime('%Y%m%d')}开放事件关键词"
                now_ts = int(time.time())

                row = conn.execute(
                    text(
                        "SELECT id, keywords FROM daily_topics "
                        "WHERE topic_id = :tid AND extract_date = :d"
                    ),
                    {"tid": topic_id, "d": extract_date},
                ).first()

                if row:
                    try:
                        old_titles = json.loads(row.keywords) if row.keywords else []
                        if not isinstance(old_titles, list):
                            old_titles = []
                    except json.JSONDecodeError:
                        old_titles = []

                    merged = _merge_titles(old_titles, new_titles)
                    keywords_json = json.dumps(merged, ensure_ascii=False)
                    conn.execute(
                        text(
                            "UPDATE daily_topics SET "
                            "keywords = :k, topic_name = :tn, "
                            "last_modify_ts = :ts, processing_status = :st "
                            "WHERE topic_id = :tid AND extract_date = :d"
                        ),
                        {
                            "k": keywords_json,
                            "tn": topic_name,
                            "ts": now_ts,
                            "st": "pending",
                            "tid": topic_id,
                            "d": extract_date,
                        },
                    )
                    logger.info(
                        "MySQL 更新 %s topic_id=%s keywords=%d",
                        date_str,
                        topic_id,
                        len(merged),
                    )
                else:
                    keywords_json = json.dumps(new_titles, ensure_ascii=False)
                    conn.execute(
                        text(
                            "INSERT INTO daily_topics ("
                            "topic_id, topic_name, topic_description, keywords, "
                            "extract_date, processing_status, add_ts, last_modify_ts"
                            ") VALUES ("
                            ":tid, :tn, :desc, :k, :d, :st, :ts, :ts)"
                        ),
                        {
                            "tid": topic_id,
                            "tn": topic_name,
                            "desc": "来自对外开放事件列表",
                            "k": keywords_json,
                            "d": extract_date,
                            "st": "pending",
                            "ts": now_ts,
                        },
                    )
                    logger.info(
                        "MySQL 插入 %s topic_id=%s keywords=%d",
                        date_str,
                        topic_id,
                        len(new_titles),
                    )

                ok_dates.append(date_str)

        return ok_dates

    def _upsert_dm(self, grouped: Dict[str, List[str]]) -> List[str]:
        ok_dates: List[str] = []
        assert self.dm_conn is not None

        for date_str, titles in sorted(grouped.items()):
            new_titles = [t.strip() for t in titles if t and t.strip()]
            if not new_titles:
                continue

            extract_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            topic_id = f"summary_{extract_date.strftime('%Y%m%d')}"
            topic_name = f"{extract_date.strftime('%Y%m%d')}开放事件关键词"
            now_ts = int(time.time())

            rows = self.dm_conn.execute(
                'SELECT "id", "keywords" FROM "daily_topics" '
                'WHERE "topic_id" = ? AND "extract_date" = ?',
                (topic_id, extract_date),
            )

            if rows:
                raw_kw = rows[0][1]
                try:
                    old_titles = json.loads(raw_kw) if raw_kw else []
                    if not isinstance(old_titles, list):
                        old_titles = []
                except json.JSONDecodeError:
                    old_titles = []

                merged = _merge_titles(old_titles, new_titles)
                keywords_json = json.dumps(merged, ensure_ascii=False)
                self.dm_conn.execute(
                    'UPDATE "daily_topics" SET '
                    '"keywords"=?, "topic_name"=?, '
                    '"last_modify_ts"=?, "processing_status"=? '
                    'WHERE "topic_id"=? AND "extract_date"=?',
                    (
                        keywords_json,
                        topic_name,
                        now_ts,
                        "pending",
                        topic_id,
                        extract_date,
                    ),
                )
                logger.info(
                    "达梦 更新 %s topic_id=%s keywords=%d",
                    date_str,
                    topic_id,
                    len(merged),
                )
            else:
                keywords_json = json.dumps(new_titles, ensure_ascii=False)
                self.dm_conn.execute(
                    'INSERT INTO "daily_topics" ('
                    '"topic_id","topic_name","topic_description","keywords",'
                    '"extract_date","processing_status","add_ts","last_modify_ts"'
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (
                        topic_id,
                        topic_name,
                        "来自对外开放事件列表",
                        keywords_json,
                        extract_date,
                        "pending",
                        now_ts,
                        now_ts,
                    ),
                )
                logger.info(
                    "达梦 插入 %s topic_id=%s keywords=%d",
                    date_str,
                    topic_id,
                    len(new_titles),
                )

            ok_dates.append(date_str)

        return ok_dates