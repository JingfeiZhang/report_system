"""event2 会话状态：按 session_id 保存报告与对话历史。"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from singleAgent.config import SESSION_MAX_MESSAGES
from singleAgent.history_client import SessionHistoryClient


PENDING_TEMPLATE_TTL_SEC = 10 * 60
PENDING_TEMPLATE_CLEANUP_INTERVAL_SEC = 60


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ConversationEntry:
    id: str
    role: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_text)


@dataclass
class PendingTemplateState:
    request_id: str
    instruction: str
    knowledge_base_ids: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time()


class PendingTemplateError(ValueError):
    """Raised when a template generation request cannot consume pending input."""


@dataclass
class SessionState:
    session_id: str
    user_id: Optional[int] = None
    agent_id: Optional[int] = 5
    agent_name: str = "舆情事件"
    title: str = ""
    conversations: List[ConversationEntry] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_time: str = field(default_factory=_now_text)
    updated_time: str = field(default_factory=_now_text)
    last_report_md: str = ""
    last_topic: str = ""
    last_search_summary: str = ""
    last_kb_summary: str = ""
    last_kb_ids: List[str] = field(default_factory=list)
    messages: List[Dict[str, str]] = field(default_factory=list)
    pending_template: Optional[PendingTemplateState] = None
    updated_at: float = 0.0

    @property
    def has_report(self) -> bool:
        return bool(self.last_report_md.strip())

    @property
    def message_count(self) -> int:
        return len(self.conversations)

    def to_detail(self) -> Dict[str, Any]:
        data = asdict(self)
        data["message_count"] = self.message_count
        data["metadata"] = _public_session_metadata(self)
        return data

    def to_summary(self) -> Dict[str, Any]:
        pending = self.pending_template
        pending_summary = None
        if pending is not None and not pending.expired:
            pending_summary = {
                "request_id": pending.request_id,
                "expires_at": pending.expires_at,
            }
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "title": self.title or self.last_topic or "舆情事件分析",
            "message_count": self.message_count,
            "created_time": self.created_time,
            "updated_time": self.updated_time,
            "metadata": _public_session_metadata(self),
            "awaiting_template": pending_summary is not None,
            "pending_template": pending_summary,
        }


def _safe_session_filename(session_id: str) -> str:
    safe = re.sub(r"[^\w\-.]", "_", session_id.strip())[:120]
    return safe or "default"


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _conversation_from_dict(item: Dict[str, Any]) -> ConversationEntry:
    return ConversationEntry(
        id=str(item.get("id") or uuid.uuid4()),
        role=str(item.get("role") or ""),
        content=str(item.get("content") or ""),
        metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        created_at=str(item.get("created_at") or _now_text()),
    )


def _pending_template_from_dict(value: Any) -> Optional[PendingTemplateState]:
    if not isinstance(value, dict):
        return None
    request_id = str(value.get("request_id") or "").strip()
    instruction = str(value.get("instruction") or "").strip()
    if not request_id or not instruction:
        return None
    try:
        created_at = float(value.get("created_at") or time.time())
        expires_at = float(value.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None
    return PendingTemplateState(
        request_id=request_id,
        instruction=instruction,
        knowledge_base_ids=[
            str(item).strip()
            for item in (value.get("knowledge_base_ids") or [])
            if str(item).strip()
        ],
        created_at=created_at,
        expires_at=expires_at,
    )


def _state_from_detail(key: str, data: Dict[str, Any]) -> SessionState:
    if isinstance(data.get("data"), dict) and (
        data["data"].get("session_id") or data["data"].get("conversations") is not None
    ):
        data = data["data"]

    conversations = []
    for item in data.get("conversations") or []:
        if isinstance(item, dict):
            conversations.append(_conversation_from_dict(item))

    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    # 清理旧 DM 行号等内部字段
    if isinstance(metadata, dict):
        metadata = {
            k: v
            for k, v in metadata.items()
            if not str(k).startswith("_history_")
        }

    return SessionState(
        session_id=str(data.get("session_id") or key),
        user_id=_coerce_int(data.get("user_id")),
        agent_id=_coerce_int(data.get("agent_id"), 5),
        agent_name=data.get("agent_name") or "舆情事件",
        title=data.get("title") or "",
        conversations=conversations,
        metadata=metadata,
        created_time=data.get("created_time") or _now_text(),
        updated_time=data.get("updated_time") or _now_text(),
        last_report_md=data.get("last_report_md") or "",
        last_topic=data.get("last_topic") or "",
        last_search_summary=data.get("last_search_summary") or "",
        last_kb_summary=data.get("last_kb_summary") or "",
        last_kb_ids=[str(x).strip() for x in (data.get("last_kb_ids") or []) if str(x).strip()],
        messages=data.get("messages") or [],
        pending_template=_pending_template_from_dict(data.get("pending_template")),
        updated_at=float(data.get("updated_at") or 0),
    )


def _conversation_role_to_message_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("human", "user"):
        return "user"
    if r in ("ai", "assistant"):
        return "assistant"
    return r or "assistant"


def build_recent_conversation_context(
    session: SessionState,
    *,
    exclude_request_id: str = "",
    limit: int = 12,
) -> List[Dict[str, str]]:
    """Build normalized recent turns without duplicating the current request."""
    request_key = (exclude_request_id or "").strip()
    context: List[Dict[str, str]] = []
    conversations = session.conversations or []
    if conversations:
        for item in conversations:
            role = _conversation_role_to_message_role(getattr(item, "role", ""))
            if role not in ("user", "assistant"):
                continue
            metadata = getattr(item, "metadata", None)
            item_request_id = (
                str(metadata.get("request_id") or "").strip()
                if isinstance(metadata, dict)
                else ""
            )
            if request_key and item_request_id == request_key:
                continue
            content = str(getattr(item, "content", "") or "").strip()
            if content:
                context.append({"role": role, "content": content})
        return context[-max(1, int(limit)):]

    for item in session.messages or []:
        if not isinstance(item, dict):
            continue
        role = _conversation_role_to_message_role(str(item.get("role") or ""))
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if content:
            context.append({"role": role, "content": content})
    return context[-max(1, int(limit)):]


def _message_role_to_conversation_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("user", "human"):
        return "human"
    if r in ("assistant", "ai"):
        return "ai"
    return r or "ai"


def _normalize_dual_tracks(state: SessionState) -> SessionState:
    """打通 messages(LLM) 与 conversations(前端历史) 双边互补。"""
    if not state.conversations and state.messages:
        for item in state.messages:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            state.conversations.append(
                ConversationEntry(
                    id=str(uuid.uuid4()),
                    role=_message_role_to_conversation_role(str(item.get("role") or "")),
                    content=content,
                    metadata={},
                )
            )

    if not state.messages and state.conversations:
        rebuilt: List[Dict[str, str]] = []
        for entry in state.conversations:
            content = (entry.content or "").strip()
            if not content:
                continue
            rebuilt.append(
                {
                    "role": _conversation_role_to_message_role(entry.role),
                    "content": content,
                }
            )
        if len(rebuilt) > SESSION_MAX_MESSAGES:
            rebuilt = rebuilt[-SESSION_MAX_MESSAGES:]
        state.messages = rebuilt

    return state


def _public_session_metadata(state: SessionState) -> Dict[str, Any]:
    """会话级 metadata：给前端可读字段，避免只有 conversation[].metadata。"""
    meta = dict(state.metadata) if isinstance(state.metadata, dict) else {}
    last_ai = meta.get("last_ai_metadata") if isinstance(meta.get("last_ai_metadata"), dict) else {}
    last_request = meta.get("last_request") if isinstance(meta.get("last_request"), dict) else {}

    if last_ai:
        meta.setdefault("module_name", last_ai.get("module_name") or "舆情事件分析")
        if "last_mode" not in meta and last_ai.get("mode") is not None:
            meta["last_mode"] = last_ai.get("mode")
        if "last_mode_name" not in meta and last_ai.get("mode_name"):
            meta["last_mode_name"] = last_ai.get("mode_name")
        if "markdown_ready" not in meta:
            meta["markdown_ready"] = bool(last_ai.get("markdown_ready"))
        if "search" not in meta and isinstance(last_ai.get("search"), dict):
            meta["search"] = last_ai.get("search")
        if "knowledge" not in meta and isinstance(last_ai.get("knowledge"), dict):
            meta["knowledge"] = last_ai.get("knowledge")
        if "reports" not in meta and isinstance(last_ai.get("reports"), list):
            meta["reports"] = last_ai.get("reports")
        if "sse" not in meta and isinstance(last_ai.get("sse"), dict):
            meta["sse"] = last_ai.get("sse")

    if last_request:
        meta.setdefault("module_name", last_request.get("module_name") or "舆情事件分析")
        meta.setdefault("last_request", last_request)

    meta.setdefault("module_name", "舆情事件分析")
    meta["message_count"] = len(state.conversations or [])
    meta["has_report"] = bool((state.last_report_md or "").strip())
    # 不对外暴露内部同步字段
    return {k: v for k, v in meta.items() if not str(k).startswith("_")}


class SessionStore:
    def __init__(
        self,
        persist_dir: Optional[Path] = None,
        history_client: Optional[SessionHistoryClient] = None,
        start_pending_cleanup: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, SessionState] = {}
        self._persist_dir = persist_dir
        self._history_client = history_client or SessionHistoryClient.from_env()
        self._history_queue: queue.Queue[
            tuple[str, str, str, Dict[str, Any]]
        ] = queue.Queue()
        self._history_worker: Optional[threading.Thread] = None
        self._history_worker_lock = threading.Lock()
        self._pending_cleanup_thread: Optional[threading.Thread] = None
        if self._persist_dir:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
        if start_pending_cleanup:
            self._pending_cleanup_thread = threading.Thread(
                target=self._pending_cleanup_loop,
                name="event2-pending-template-cleanup",
                daemon=True,
            )
            self._pending_cleanup_thread.start()

    def get(self, session_id: str) -> SessionState:
        key = (session_id or "default").strip() or "default"
        with self._lock:
            local_state = _normalize_dual_tracks(self._get_or_load(key))
            if self._clear_expired_pending_locked(key, local_state):
                self._persist(key, local_state)
            self._cache[key] = local_state
            return local_state

    def save_pending_template(
        self,
        session_id: str,
        *,
        request_id: str,
        instruction: str,
        knowledge_base_ids: Optional[List[str]] = None,
    ) -> PendingTemplateState:
        key = (session_id or "default").strip() or "default"
        request_key = (request_id or "").strip()
        text = (instruction or "").strip()
        if not request_key or not text:
            raise PendingTemplateError("待生成内容和 request_id 不能为空")
        now = time.time()
        pending = PendingTemplateState(
            request_id=request_key,
            instruction=text,
            knowledge_base_ids=list(knowledge_base_ids or []),
            created_at=now,
            expires_at=now + PENDING_TEMPLATE_TTL_SEC,
        )
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            state.pending_template = pending
            state.updated_at = now
            state.updated_time = _now_text()
            self._persist(key, state)
        return pending

    def consume_pending_template(
        self,
        session_id: str,
        *,
        request_id: str,
    ) -> PendingTemplateState:
        key = (session_id or "default").strip() or "default"
        source_key = (request_id or "").strip()
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            pending = state.pending_template
            if pending is None:
                raise PendingTemplateError("待生成内容不存在或已过期，请重新提交报告主题")
            if pending.expired:
                state.pending_template = None
                self._persist(key, state)
                raise PendingTemplateError("待生成内容已过期，请重新提交报告主题")
            if not source_key or source_key != pending.request_id:
                raise PendingTemplateError("source_request_id 与待选模板请求不匹配")
            state.pending_template = None
            state.updated_at = time.time()
            state.updated_time = _now_text()
            self._persist(key, state)
            return pending

    def cleanup_expired_pending(self) -> int:
        cleared = 0
        with self._lock:
            keys = set(self._cache.keys())
            if self._persist_dir and self._persist_dir.exists():
                keys.update(path.stem for path in self._persist_dir.glob("*.json"))
            for key in keys:
                state = self._get_or_load(key)
                if self._clear_expired_pending_locked(key, state):
                    self._persist(key, state)
                    cleared += 1
        return cleared

    @staticmethod
    def _clear_expired_pending_locked(key: str, state: SessionState) -> bool:
        del key  # Documents that callers already hold the store lock for this state.
        if state.pending_template is None or not state.pending_template.expired:
            return False
        state.pending_template = None
        state.updated_at = time.time()
        state.updated_time = _now_text()
        return True

    def _pending_cleanup_loop(self) -> None:
        while True:
            time.sleep(PENDING_TEMPLATE_CLEANUP_INTERVAL_SEC)
            try:
                self.cleanup_expired_pending()
            except Exception as error:
                logger.warning(f"清理过期待选模板失败: {error}")

    def update_session_info(
        self,
        session_id: str,
        *,
        user_id: Optional[int] = None,
        agent_id: Optional[int] = None,
        agent_name: Optional[str] = None,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionState:
        key = (session_id or "default").strip() or "default"
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            if user_id is not None:
                state.user_id = user_id
            if agent_id is not None:
                state.agent_id = agent_id
            if agent_name:
                state.agent_name = agent_name
            if title and not state.title:
                state.title = title
            if metadata:
                state.metadata.update(metadata)
            state.updated_at = time.time()
            state.updated_time = _now_text()
            self._persist(key, state)
            return state

    def append_conversation(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
        sync_platform: bool = True,
    ) -> ConversationEntry:
        key = (session_id or "default").strip() or "default"
        entry = ConversationEntry(
            id=conversation_id or str(uuid.uuid4()),
            role=role,
            content=(content or "").strip(),
            metadata=metadata or {},
        )
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            state.conversations.append(entry)
            msg_role = _conversation_role_to_message_role(entry.role)
            text = entry.content
            if text:
                last = state.messages[-1] if state.messages else None
                if not (
                    isinstance(last, dict)
                    and last.get("role") == msg_role
                    and (last.get("content") or "").strip() == text
                ):
                    state.messages.append({"role": msg_role, "content": text})
                    if len(state.messages) > SESSION_MAX_MESSAGES:
                        state.messages = state.messages[-SESSION_MAX_MESSAGES:]
            state.updated_at = time.time()
            state.updated_time = entry.created_at
            # 锁内只做 to_detail 快照；落盘与平台入队移出锁，避免慢 IO 拖死其它请求
            detail = state.to_detail()
            enqueue = bool(sync_platform and entry.content)
            meta_copy = (
                dict(entry.metadata) if isinstance(entry.metadata, dict) else {}
            )
        self._persist_detail(key, detail)
        if enqueue:
            self._enqueue_platform_message(
                session_id=key,
                role=entry.role,
                content=entry.content,
                metadata=meta_copy,
            )
        return entry

    def record_human_turn(
        self,
        session_id: str,
        *,
        content: str,
        user_id: Optional[int],
        agent_id: int,
        agent_name: str,
        title: str,
        metadata: Dict[str, Any],
    ) -> ConversationEntry:
        self.update_session_info(
            session_id,
            user_id=user_id,
            agent_id=agent_id,
            agent_name=agent_name,
            title=title,
            metadata={
                "module_name": metadata.get("module_name", "舆情事件分析"),
                "last_request": metadata,
            },
        )
        return self.append_conversation(
            session_id,
            role="human",
            content=content,
            metadata=metadata,
            sync_platform=True,
        )

    def record_ai_turn(
        self,
        session_id: str,
        *,
        content: str,
        metadata: Dict[str, Any],
    ) -> ConversationEntry:
        entry = self.append_conversation(
            session_id,
            role="ai",
            content=content,
            metadata=metadata,
            sync_platform=True,
        )
        key = (session_id or "default").strip() or "default"
        with self._lock:
            state = self._get_or_load(key)
            state.metadata.update({
                "module_name": metadata.get("module_name") or state.metadata.get("module_name") or "舆情事件分析",
                "last_ai_metadata": metadata,
                "last_mode": metadata.get("mode"),
                "last_mode_name": metadata.get("mode_name"),
                "last_markdown_ready": metadata.get("markdown_ready", False),
                "markdown_ready": bool(metadata.get("markdown_ready")),
                "search": metadata.get("search") if isinstance(metadata.get("search"), dict) else state.metadata.get("search"),
                "knowledge": metadata.get("knowledge") if isinstance(metadata.get("knowledge"), dict) else state.metadata.get("knowledge"),
                "reports": metadata.get("reports") if isinstance(metadata.get("reports"), list) else state.metadata.get("reports"),
                "sse": metadata.get("sse") if isinstance(metadata.get("sse"), dict) else state.metadata.get("sse"),
            })
            state.updated_time = entry.created_at
            detail = state.to_detail()
        self._persist_detail(key, detail)
        return entry

    def save_report(
        self,
        session_id: str,
        topic: str,
        markdown: str,
        search_summary: str = "",
        kb_summary: str = "",
        kb_ids: Optional[List[str]] = None,
        request_id: str = "",
    ) -> None:
        key = (session_id or "default").strip() or "default"
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            from singleAgent.search_planner import normalize_topic

            value = normalize_topic(topic)
            latest = state.metadata.get("last_request")
            latest_request_id = (
                str(latest.get("request_id") or "").strip()
                if isinstance(latest, dict)
                else ""
            )
            request_key = (request_id or "").strip()
            if value and (not request_key or latest_request_id == request_key):
                state.last_topic = value
            state.last_report_md = markdown
            if search_summary:
                state.last_search_summary = search_summary
            if kb_summary:
                state.last_kb_summary = kb_summary
            if kb_ids is not None:
                state.last_kb_ids = list(kb_ids)
            state.updated_at = time.time()
            state.updated_time = _now_text()
            self._persist(key, state)

    def update_last_topic(self, session_id: str, topic: str) -> None:
        key = (session_id or "default").strip() or "default"
        from singleAgent.search_planner import normalize_topic

        value = normalize_topic(topic)
        if not value:
            return
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            state.last_topic = value
            state.updated_at = time.time()
            state.updated_time = _now_text()
            self._persist(key, state)

    def update_last_topic_if_current(
        self,
        session_id: str,
        topic: str,
        *,
        request_id: str,
    ) -> bool:
        """Write a valid topic only when request_id is still the latest request."""
        from singleAgent.search_planner import normalize_topic

        key = (session_id or "default").strip() or "default"
        value = normalize_topic(topic)
        if not value:
            return False
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            latest = state.metadata.get("last_request")
            latest_request_id = (
                str(latest.get("request_id") or "").strip()
                if isinstance(latest, dict)
                else ""
            )
            request_key = (request_id or "").strip()
            if request_key and latest_request_id != request_key:
                logger.info(
                    f"skip stale topic update session={key} request={request_key} "
                    f"latest_request={latest_request_id}"
                )
                return False
            state.last_topic = value
            state.updated_at = time.time()
            state.updated_time = _now_text()
            self._persist(key, state)
            return True

    def append_message(self, session_id: str, role: str, content: str) -> None:
        """仅维护本地 LLM messages，不写平台（避免与 record_* 重复）。"""
        key = (session_id or "default").strip() or "default"
        text = (content or "").strip()
        if not text:
            return
        self.get(key)
        with self._lock:
            state = self._get_or_load(key)
            last = state.messages[-1] if state.messages else None
            if not (
                isinstance(last, dict)
                and last.get("role") == role
                and (last.get("content") or "").strip() == text
            ):
                state.messages.append({"role": role, "content": text})
                if len(state.messages) > SESSION_MAX_MESSAGES:
                    state.messages = state.messages[-SESSION_MAX_MESSAGES:]
            state.updated_at = time.time()
            state.updated_time = _now_text()
            self._persist(key, state)

    def list_sessions(
        self,
        *,
        agent_id: Optional[int] = None,
        page_no: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        page_no = max(1, page_no)
        page_size = min(max(1, page_size), 100)

        with self._lock:
            keys = set(self._cache.keys())
            if self._persist_dir and self._persist_dir.exists():
                keys.update(path.stem for path in self._persist_dir.glob("*.json"))
            states = [self._get_or_load(key) for key in keys]

        if agent_id is not None:
            states = [s for s in states if s.agent_id == agent_id]

        states.sort(
            key=lambda s: (
                s.updated_at or 0,
                s.updated_time or "",
                s.created_time or "",
            ),
            reverse=True,
        )
        total = len(states)
        start = (page_no - 1) * page_size
        end = start + page_size
        return {
            "list": [s.to_summary() for s in states[start:end]],
            "total": total,
            "page_no": page_no,
            "page_size": page_size,
        }

    def _get_or_load(self, key: str) -> SessionState:
        if key in self._cache:
            return self._cache[key]
        state = self._load_from_disk(key)
        self._cache[key] = state
        return state

    def _persist(self, key: str, state: SessionState) -> None:
        self._persist_detail(key, state.to_detail())

    def _persist_detail(self, key: str, detail: Dict[str, Any]) -> None:
        if not self._persist_dir:
            return
        path = self._persist_dir / f"{_safe_session_filename(key)}.json"
        try:
            path.write_text(
                json.dumps(detail, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"会话落盘失败 session={key}: {e}")

    def _persist_disk(self, key: str, state: SessionState) -> None:
        self._persist_detail(key, state.to_detail())

    def _push_platform_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        metadata: Dict[str, Any],
    ) -> None:
        client = self._history_client
        if not client:
            return
        # metadata 必填；去掉过大或不必要字段，避免拖垮接口
        safe_meta = _platform_metadata(metadata)
        client.append_message(
            session_id=session_id,
            role=role,
            content=content,
            metadata=safe_meta,
        )

    def _enqueue_platform_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        metadata: Dict[str, Any],
    ) -> None:
        client = self._history_client
        if not client or not client.available:
            return
        self._history_queue.put((session_id, role, content, dict(metadata)))
        with self._history_worker_lock:
            if self._history_worker is not None and self._history_worker.is_alive():
                return
            self._history_worker = threading.Thread(
                target=self._drain_platform_messages,
                name="event2-history-sync",
                daemon=True,
            )
            self._history_worker.start()

    def _drain_platform_messages(self) -> None:
        while True:
            session_id, role, content, metadata = self._history_queue.get()
            try:
                self._push_platform_message(
                    session_id=session_id,
                    role=role,
                    content=content,
                    metadata=metadata,
                )
            except Exception as error:
                logger.warning(
                    f"event2 history worker failed session={session_id} "
                    f"role={role}: {error}"
                )
            finally:
                self._history_queue.task_done()

    def _load_from_disk(self, key: str) -> SessionState:
        if not self._persist_dir:
            return SessionState(session_id=key)
        path = self._persist_dir / f"{_safe_session_filename(key)}.json"
        if not path.is_file():
            return SessionState(session_id=key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _normalize_dual_tracks(_state_from_detail(key, data))
        except Exception as e:
            logger.warning(f"会话读取失败 session={key}: {e}")
            return SessionState(session_id=key)


def _platform_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """写入平台的 metadata：保留关键业务字段，去掉体积过大的嵌套。"""
    if not isinstance(metadata, dict):
        return {}
    keep_keys = (
        "module_name",
        "mode",
        "mode_name",
        "markdown_ready",
        "knowledge_base_ids",
        "selected_template_id",
        "request_id",
        "source_request_id",
        "turn_status",
        "cancelled",
        "cancel_reason",
        "resolved_topic",
        "topic_source",
        "ismodel",
        "user_id",
        "title",
        # 前端历史回显附件：conversations.metadata.filesData
        "filesData",
        "files_data",
        # 历史页回放正文 (标N)：认知攻防等模块用 citations；tips 为 SSE 同构备份
        "tips",
        "citations",
        "citation_count",
    )
    out: Dict[str, Any] = {}
    for key in keep_keys:
        if key in metadata and metadata[key] is not None:
            out[key] = metadata[key]
    # last_request 里常有完整请求字段
    last_request = metadata.get("last_request")
    if isinstance(last_request, dict):
        for key in ("knowledge_base_ids", "selected_template_id", "ismodel", "filesData", "files_data"):
            if key not in out and last_request.get(key) is not None:
                out[key] = last_request.get(key)
    knowledge = metadata.get("knowledge")
    if isinstance(knowledge, dict) and "knowledge_base_ids" not in out:
        if knowledge.get("knowledge_base_ids") is not None:
            out["knowledge_base_ids"] = knowledge.get("knowledge_base_ids")
    return out


_store: Optional[SessionStore] = None


def get_session_store(project_root: Optional[Path] = None) -> SessionStore:
    global _store
    if _store is None:
        root = project_root or Path(__file__).resolve().parent.parent
        _store = SessionStore(
            persist_dir=root / "logs" / "sessions",
            history_client=SessionHistoryClient.from_env(),
            start_pending_cleanup=True,
        )
    return _store
