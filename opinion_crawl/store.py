"""厂家舆情采集任务本地库（SQLite）。

主键：event_type + 对应业务 ID（monitor→main_event_id，record→record_event_id）。
进度权威在本库；左屏不存采集进度。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_LOCK = threading.RLock()

VALID_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped"})
VALID_EVENT_TYPES = frozenset({"monitor", "record"})


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    raw = (os.getenv("OPINION_CRAWL_DB_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _project_root() / "logs" / "opinion_crawl" / "tasks.db"


@dataclass
class CrawlTask:
    event_type: str
    event_id: int
    event_date: str
    title: str = ""
    summary: str = ""
    place_name: str = ""
    region: str = ""
    topic_id: str = ""
    status: str = "pending"
    message: str = ""
    keywords: List[str] = field(default_factory=list)
    raw_item: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def main_event_id(self) -> Optional[int]:
        return self.event_id if self.event_type == "monitor" else None

    @property
    def record_event_id(self) -> Optional[int]:
        return self.event_id if self.event_type == "record" else None

    def to_progress_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "main_event_id": self.main_event_id,
            "record_event_id": self.record_event_id,
            "id": self.event_id,
            "event_date": self.event_date,
            "title": self.title,
            "status": self.status,
            "message": self.message or None,
            "keywords": list(self.keywords),
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_to_task(row: sqlite3.Row) -> CrawlTask:
    keywords_raw = row["keywords_json"] or "[]"
    raw_item_raw = row["raw_item_json"] or "{}"
    try:
        keywords = json.loads(keywords_raw)
    except json.JSONDecodeError:
        keywords = []
    try:
        raw_item = json.loads(raw_item_raw)
    except json.JSONDecodeError:
        raw_item = {}
    return CrawlTask(
        event_type=row["event_type"],
        event_id=int(row["event_id"]),
        event_date=row["event_date"] or "",
        title=row["title"] or "",
        summary=row["summary"] or "",
        place_name=row["place_name"] or "",
        region=row["region"] or "",
        topic_id=row["topic_id"] or "",
        status=row["status"] or "pending",
        message=row["message"] or "",
        keywords=list(keywords) if isinstance(keywords, list) else [],
        raw_item=dict(raw_item) if isinstance(raw_item, dict) else {},
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


class CrawlTaskStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        with _LOCK:
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crawl_tasks (
                    event_type TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    place_name TEXT,
                    region TEXT,
                    topic_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    message TEXT,
                    keywords_json TEXT,
                    raw_item_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (event_type, event_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_crawl_tasks_date "
                "ON crawl_tasks(event_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_crawl_tasks_status "
                "ON crawl_tasks(status)"
            )

    def upsert_from_open_item(
        self,
        item: Dict[str, Any],
        *,
        crawl_day: str,
        preserve_progress: bool = True,
    ) -> CrawlTask:
        event_type = str(item.get("event_type") or "").strip().lower()
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"非法 event_type: {event_type}")

        if event_type == "monitor":
            event_id = item.get("main_event_id")
            if event_id is None:
                event_id = item.get("id")
        else:
            event_id = item.get("record_event_id")
            if event_id is None:
                event_id = item.get("id")
        if event_id is None:
            raise ValueError("事件缺少业务 ID")
        event_id = int(event_id)

        keywords = _keywords_from_item(item)
        now = _now()
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        place_name = str(item.get("place_name") or "")
        region = str(item.get("region") or "")
        topic_id = str(item.get("topic_id") or "")
        # 落库日：厂家按天拉取所用日期（文档：event_date 为采集日）
        event_date = crawl_day

        with self._conn() as conn:
            existing = conn.execute(
                "SELECT status, message, created_at FROM crawl_tasks "
                "WHERE event_type=? AND event_id=?",
                (event_type, event_id),
            ).fetchone()
            if existing and preserve_progress:
                status = existing["status"]
                message = existing["message"] or ""
                created_at = existing["created_at"] or now
            else:
                status = "pending"
                message = ""
                created_at = now

            conn.execute(
                """
                INSERT INTO crawl_tasks (
                    event_type, event_id, event_date, title, summary,
                    place_name, region, topic_id, status, message,
                    keywords_json, raw_item_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_type, event_id) DO UPDATE SET
                    event_date=excluded.event_date,
                    title=excluded.title,
                    summary=excluded.summary,
                    place_name=excluded.place_name,
                    region=excluded.region,
                    topic_id=excluded.topic_id,
                    keywords_json=excluded.keywords_json,
                    raw_item_json=excluded.raw_item_json,
                    updated_at=excluded.updated_at,
                    status=CASE
                        WHEN ? THEN crawl_tasks.status
                        ELSE excluded.status
                    END,
                    message=CASE
                        WHEN ? THEN crawl_tasks.message
                        ELSE excluded.message
                    END
                """,
                (
                    event_type,
                    event_id,
                    event_date,
                    title,
                    summary,
                    place_name,
                    region,
                    topic_id,
                    status,
                    message,
                    json.dumps(keywords, ensure_ascii=False),
                    json.dumps(item, ensure_ascii=False),
                    created_at,
                    now,
                    1 if preserve_progress else 0,
                    1 if preserve_progress else 0,
                ),
            )
        task = self.get(event_type, event_id)
        assert task is not None
        return task

    def get(self, event_type: str, event_id: int) -> Optional[CrawlTask]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM crawl_tasks WHERE event_type=? AND event_id=?",
                (event_type, int(event_id)),
            ).fetchone()
        return _row_to_task(row) if row else None

    def list_by_date(self, event_date: str) -> List[CrawlTask]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM crawl_tasks WHERE event_date=? "
                "ORDER BY event_type, event_id",
                (event_date,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def count_by_date(self, event_date: str) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM crawl_tasks "
                "WHERE event_date=? GROUP BY status",
                (event_date,),
            ).fetchall()
        counts = {s: 0 for s in ("pending", "running", "completed", "failed", "skipped")}
        total = 0
        for row in rows:
            status = row["status"] or "pending"
            n = int(row["n"])
            total += n
            if status in counts:
                counts[status] = n
            else:
                counts["pending"] = counts.get("pending", 0) + n
        pending_like = counts["pending"] + counts["running"] + counts["skipped"]
        return {
            "total": total,
            "completed": counts["completed"],
            "pending": pending_like,
            "failed": counts["failed"],
            "running": counts["running"],
            "skipped": counts["skipped"],
            "raw_pending": counts["pending"],
        }

    def update_status(
        self,
        event_type: str,
        event_id: int,
        *,
        status: str,
        message: str = "",
    ) -> Optional[CrawlTask]:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法 status: {status}")
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE crawl_tasks SET status=?, message=?, updated_at=? "
                "WHERE event_type=? AND event_id=?",
                (status, message, now, event_type, int(event_id)),
            )
            if cur.rowcount == 0:
                return None
        return self.get(event_type, event_id)

    def update_status_by_event_ids(
        self,
        event_date: str,
        event_ids: List[int],
        *,
        status: str,
        message: str = "",
    ) -> int:
        """按采集日 + 业务事件 ID 批量更新状态（不区分 monitor/record）。"""
        if status not in VALID_STATUSES:
            raise ValueError(f"非法 status: {status}")
        ids = sorted({int(x) for x in event_ids})
        if not ids:
            return 0
        now = _now()
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE crawl_tasks SET status=?, message=?, updated_at=? "
                f"WHERE event_date=? AND event_id IN ({placeholders})",
                (status, message, now, event_date, *ids),
            )
            return int(cur.rowcount)

    def update_status_by_date(
        self,
        event_date: str,
        *,
        status: str,
        message: str = "",
        only_statuses: Optional[List[str]] = None,
    ) -> int:
        """批量更新某采集日任务状态，返回更新行数。"""
        if status not in VALID_STATUSES:
            raise ValueError(f"非法 status: {status}")
        now = _now()
        with self._conn() as conn:
            if only_statuses:
                placeholders = ",".join("?" for _ in only_statuses)
                cur = conn.execute(
                    f"UPDATE crawl_tasks SET status=?, message=?, updated_at=? "
                    f"WHERE event_date=? AND status IN ({placeholders})",
                    (status, message, now, event_date, *only_statuses),
                )
            else:
                cur = conn.execute(
                    "UPDATE crawl_tasks SET status=?, message=?, updated_at=? "
                    "WHERE event_date=?",
                    (status, message, now, event_date),
                )
            return int(cur.rowcount or 0)

    def update_status_by_titles(
        self,
        event_date: str,
        titles: List[str],
        *,
        status: str,
        message: str = "",
    ) -> int:
        """按采集日 + 标题匹配更新状态（外网 event_sync 回写用）。"""
        if status not in VALID_STATUSES:
            raise ValueError(f"非法 status: {status}")
        wanted = {str(t).strip() for t in titles if str(t).strip()}
        if not wanted:
            return 0
        now = _now()
        updated = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_type, event_id, title FROM crawl_tasks WHERE event_date=?",
                (event_date,),
            ).fetchall()
            for row in rows:
                title = (row["title"] or "").strip()
                # 标题或截断关键词命中均可
                hit = title in wanted or any(
                    title.startswith(w) or w.startswith(title[:40]) for w in wanted
                )
                if not hit:
                    continue
                conn.execute(
                    "UPDATE crawl_tasks SET status=?, message=?, updated_at=? "
                    "WHERE event_type=? AND event_id=?",
                    (status, message, now, row["event_type"], int(row["event_id"])),
                )
                updated += 1
        return updated

    def list_pending_by_date(
        self, event_date: str, *, limit: int = 200
    ) -> List[CrawlTask]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM crawl_tasks WHERE event_date=? AND status IN "
                "('pending','failed') ORDER BY event_type, event_id LIMIT ?",
                (event_date, int(limit)),
            ).fetchall()
        return [_row_to_task(r) for r in rows]


def _keywords_from_item(item: Dict[str, Any]) -> List[str]:
    """从开放事件字段粗抽关键词，供后续 MindSpider 使用。"""
    parts: List[str] = []
    for key in ("title", "place_name", "summary"):
        val = str(item.get(key) or "").strip()
        if val:
            parts.append(val)
    # 标题往往足够；summary 截短
    kws: List[str] = []
    title = str(item.get("title") or "").strip()
    if title:
        kws.append(title[:40])
    place = str(item.get("place_name") or "").strip()
    if place and place not in title:
        kws.append(place)
    # 去重保序
    seen = set()
    out: List[str] = []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:6]


_STORE: Optional[CrawlTaskStore] = None


def get_store() -> CrawlTaskStore:
    global _STORE
    if _STORE is None:
        _STORE = CrawlTaskStore()
    return _STORE


def reset_store_for_tests(db_path: Path) -> CrawlTaskStore:
    global _STORE
    _STORE = CrawlTaskStore(db_path)
    return _STORE
