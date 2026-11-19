"""
分析任务流程事件总线（A 层阶段进度 + agent_log 截断日志 SSE）
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Deque, Dict, List, Optional

STREAM_HEARTBEAT_INTERVAL = 15
STREAM_MAX_HISTORY = 500
STREAM_TERMINAL_TYPES = frozenset({"task_completed", "task_error"})


class TaskEventBus:
    """单任务事件缓冲与订阅分发。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._lock = threading.Lock()
        self._seq = 0
        self._history: Deque[Dict[str, Any]] = deque(maxlen=STREAM_MAX_HISTORY)
        self._subscribers: List[Queue] = []

    def emit(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """记录事件并推送给所有订阅者；失败不影响主流程。"""
        payload = payload or {}
        with self._lock:
            self._seq += 1
            event = {
                "id": self._seq,
                "type": event_type,
                "task_id": self.task_id,
                "timestamp": datetime.now().isoformat(),
                "payload": payload,
            }
            self._history.append(event)
            listeners = list(self._subscribers)

        for queue in listeners:
            try:
                queue.put(event, timeout=0.1)
            except Exception:
                pass
        return event

    def history_since(self, last_event_id: Optional[int]) -> List[Dict[str, Any]]:
        with self._lock:
            if last_event_id is None:
                return list(self._history)
            return [e for e in self._history if e.get("id", 0) > last_event_id]

    def subscribe(self) -> Queue:
        queue: Queue = Queue()
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    @staticmethod
    def format_sse(event: Dict[str, Any]) -> str:
        payload = json.dumps(event, ensure_ascii=False)
        event_id = event.get("id", 0)
        event_type = event.get("type", "message")
        return f"id: {event_id}\nevent: {event_type}\ndata: {payload}\n\n"

    @staticmethod
    def heartbeat(task_id: str, status: str) -> Dict[str, Any]:
        return {
            "id": f"hb-{int(datetime.now().timestamp() * 1000)}",
            "type": "heartbeat",
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
            "payload": {"status": status},
        }
