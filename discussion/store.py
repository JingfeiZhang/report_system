"""SQLite state and event log. All state changes and their events commit together."""
from __future__ import annotations

import json
from .storage import EvidenceStorage
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Conflict(ValueError):
    pass


class Store(EvidenceStorage):
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "discussions.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meetings (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT NOT NULL,
                    type TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_meeting ON events(meeting_id, id);
            """)

        self.migrate()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, meeting_id, summary=False):
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                raise KeyError("会议不存在")
            return self._hydrate(conn, json.loads(row[0]), summary)

    def list(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT state FROM meetings ORDER BY updated DESC LIMIT 100").fetchall()
        return [{k: s[k] for k in ("id", "title", "mode", "status", "updated_at")}
                for s in (json.loads(row[0]) for row in rows)]

    def create(self, state):
        with self.connect() as conn:
            conn.execute("INSERT INTO meetings VALUES(?,?,?)",
                         (state["id"], json.dumps(self._pack(conn, state), ensure_ascii=False), now()))
            self._event(conn, state["id"], "created", {})
        return state

    def change(self, meeting_id, fn, event="updated", payload=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                raise KeyError("会议不存在")
            state = self._hydrate(conn, json.loads(row[0]))
            fn(state)
            state["updated_at"] = now()
            conn.execute("UPDATE meetings SET state=?,updated=? WHERE id=?",
                         (json.dumps(self._pack(conn, state), ensure_ascii=False), state["updated_at"], meeting_id))
            self._event(conn, meeting_id, event, payload or {})
        return state

    @staticmethod
    def _event(conn, mid, kind, payload):
        conn.execute("INSERT INTO events(meeting_id,type,payload,created) VALUES(?,?,?,?)",
                     (mid, kind, json.dumps(payload, ensure_ascii=False), now()))

    def events(self, mid, after=0):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE meeting_id=? AND id>? ORDER BY id LIMIT 100",
                                (mid, after)).fetchall()
        return [dict(id=r["id"], type=r["type"], payload=json.loads(r["payload"]),
                     created_at=r["created"]) for r in rows]

    def recover(self):
        with self.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM meetings")]
        for mid in ids:
            if self.get(mid, summary=True)["status"] in ("planning", "running", "reporting", "collecting_images", "analyzing_images"):
                def interrupt(s):
                    s["status"] = "interrupted"
                    s["error"] = "服务重启，未完成任务已中断。可手动重试，不会自动再次调用模型。"
                    s["operation"] = None
                    if s.get('image_collection', {}).get('status') == 'running':
                        s['image_collection'].update(status='interrupted', message='服务中断；已保存图片保留，可手动继续补取')
                    if s.get('image_analysis', {}).get('status') == 'running':
                        s['image_analysis'].update(status='interrupted', message='服务中断；已识别区域保留，等待手动继续')
                    for r in s["rounds"]:
                        if r["status"] == "running":
                            r["status"] = "interrupted"
                            for task in r["tasks"]:
                                if task["status"] in ("pending", "running"):
                                    task["status"] = "interrupted"
                            for collection in r.get("collections", []):
                                if collection["status"] in ("running", "pending"):
                                    collection.update(status="interrupted", stop_reason="interrupted", resumable=collection.get('kind')!='web', message="服务重启，采集任务已中断；下轮可重试")
                    for m in s["messages"]:
                        if m["status"] == "assigned":
                            m["status"] = "pending"
                    for report in s["reports"]:
                        if report["status"] == "running":
                            report["status"] = "interrupted"
                self.change(mid, interrupt, "interrupted")
                for task in self.collection_tasks(mid):
                    if task["status"] in ("running", "pending"):
                        task.update(status="interrupted", stop_reason="interrupted", resumable=True, message="服务中断，等待手动继续")
                        self.save_collection(mid, task)

