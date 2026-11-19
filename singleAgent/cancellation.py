"""Request-scoped cancellation and same-session preemption for event2."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from loguru import logger

from singleAgent.diag_codes import (
    E2103_CANCEL_ACCEPTED,
    E2104_CLOSER_DONE,
    E2105_CLOSER_TIMEOUT,
    format_diag,
    ms_bucket,
)

# stop 时同步 close LLM 流可能卡住；超过该秒数则放后台继续关，避免拖死事件循环
CLOSER_JOIN_TIMEOUT_SEC = float(os.getenv("EVENT2_CLOSER_JOIN_TIMEOUT_SEC", "2") or "2")


class GenerationCancelled(Exception):
    """Raised when an event2 generation has been cancelled."""


class CrossSessionPreemptionError(ValueError):
    """Raised when source_request_id belongs to another session."""


@dataclass(frozen=True)
class CancelResult:
    request_id: str
    session_id: str = ""
    cancelled: bool = False
    status: str = "not_found"

    def to_dict(self) -> Dict[str, object]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "cancelled": self.cancelled,
            "status": self.status,
        }


@dataclass
class GenerationHandle:
    request_id: str
    session_id: str
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_reason: str = ""
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _terminal_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _partial_parts: list[str] = field(default_factory=list, repr=False)
    _cancel_persisted: bool = field(default=False, repr=False)
    _closers: Dict[int, Callable[[], None]] = field(default_factory=dict, repr=False)
    _next_closer_id: int = field(default=1, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def terminal(self) -> bool:
        return self._terminal_event.is_set()

    @property
    def partial_text(self) -> str:
        with self._lock:
            return "".join(self._partial_parts).strip()

    def append_partial(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._partial_parts.append(text)
            self.updated_at = time.time()

    def register_closer(self, closer: Callable[[], None]) -> int:
        """Register an upstream close callback and run it immediately if cancelled."""
        if not callable(closer):
            raise TypeError("closer must be callable")
        call_immediately = False
        with self._lock:
            token = self._next_closer_id
            self._next_closer_id += 1
            if self._cancel_event.is_set() or self._terminal_event.is_set():
                call_immediately = True
            else:
                self._closers[token] = closer
        if call_immediately:
            self._call_closer(closer)
        return token

    def unregister_closer(self, token: int) -> None:
        with self._lock:
            self._closers.pop(token, None)

    @staticmethod
    def _call_closer(closer: Callable[[], None], *, request_id: str = "", idx: int = -1) -> None:
        started = time.time()
        try:
            closer()
        except Exception as exc:
            # Cancellation must remain best-effort and must not break the API call.
            elapsed_ms = (time.time() - started) * 1000
            logger.warning(
                format_diag(
                    E2104_CLOSER_DONE,
                    "closer_error",
                    request=request_id,
                    idx=idx,
                    err_type=type(exc).__name__,
                    elapsed_ms=f"{elapsed_ms:.1f}",
                    bucket=ms_bucket(elapsed_ms),
                )
            )
            return
        elapsed_ms = (time.time() - started) * 1000
        logger.warning(
            format_diag(
                E2104_CLOSER_DONE,
                "closer_ok",
                request=request_id,
                idx=idx,
                elapsed_ms=f"{elapsed_ms:.1f}",
                bucket=ms_bucket(elapsed_ms),
            )
        )

    def request_cancel(self, reason: str = "user", *, join_closers: bool = True) -> bool:
        """请求取消。

        join_closers=False：只拉起 closer 后台线程、立即返回。
        用于：
        - 流式生成器线程内的 disconnect（避免 closer 关 stream 与生成器互相等待死锁）
        - 同会话抢占（避免在 asyncio 事件循环线程上 join 卡住整站）
        stop API 可保持默认 True，并在 to_thread 里调用。
        """
        closers: list[Callable[[], None]] = []
        with self._lock:
            if self.status in ("completed", "cancelled", "failed"):
                return False
            first_request = not self._cancel_event.is_set()
            if not first_request:
                return False
            self._cancel_event.set()
            self.cancel_reason = reason or "user"
            self.status = "cancelling"
            self.updated_at = time.time()
            closers = list(self._closers.values())
            self._closers.clear()

        logger.warning(
            format_diag(
                E2103_CANCEL_ACCEPTED,
                "request_cancel",
                request=self.request_id,
                session=self.session_id,
                reason=self.cancel_reason,
                closers=len(closers),
                join_closers=join_closers,
            )
        )
        if not closers:
            return True

        def _run_closers() -> None:
            for idx, closer in enumerate(closers):
                self._call_closer(closer, request_id=self.request_id, idx=idx)

        # 在独立线程关上游；可选限时 join
        worker = threading.Thread(
            target=_run_closers,
            name=f"event2-closer-{self.request_id[:12]}",
            daemon=True,
        )
        worker.start()
        if not join_closers:
            return True

        started = time.time()
        worker.join(timeout=max(0.1, CLOSER_JOIN_TIMEOUT_SEC))
        elapsed_ms = (time.time() - started) * 1000
        if worker.is_alive():
            logger.error(
                format_diag(
                    E2105_CLOSER_TIMEOUT,
                    "closer_still_running_bg",
                    request=self.request_id,
                    join_timeout_sec=CLOSER_JOIN_TIMEOUT_SEC,
                    elapsed_ms=f"{elapsed_ms:.1f}",
                )
            )
        return True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise GenerationCancelled(self.cancel_reason or "user")

    def mark_cancel_persisted(self) -> bool:
        with self._lock:
            if self._cancel_persisted:
                return False
            self._cancel_persisted = True
            return True

    @property
    def cancel_persisted(self) -> bool:
        with self._lock:
            return self._cancel_persisted

    def wait_terminal(self, timeout: Optional[float] = None) -> bool:
        return self._terminal_event.wait(timeout)

    def finish(self, status: str) -> None:
        with self._lock:
            if self._cancel_event.is_set():
                self.status = "cancelled"
            else:
                self.status = status
            self.updated_at = time.time()
            self._closers.clear()
            self._terminal_event.set()


@dataclass(frozen=True)
class RegistrationResult:
    handle: GenerationHandle
    preempted_handles: Tuple[GenerationHandle, ...] = ()

    @property
    def preempted_request_ids(self) -> Tuple[str, ...]:
        return tuple(item.request_id for item in self.preempted_handles)


class CancellationRegistry:
    def __init__(self, ttl_seconds: float = 1800.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._handles: Dict[str, GenerationHandle] = {}
        self._active_by_session: Dict[str, str] = {}

    @staticmethod
    def _normalize_request_id(request_id: str) -> str:
        key = (request_id or "").strip()
        if not key:
            raise ValueError("request_id cannot be empty")
        return key

    def register_or_preempt(
        self,
        request_id: str,
        session_id: str,
        *,
        source_request_id: str = "",
    ) -> RegistrationResult:
        """Register a request and cancel any running request in the same session."""
        key = self._normalize_request_id(request_id)
        session_key = (session_id or "").strip() or "default"
        source_key = (source_request_id or "").strip()
        self.cleanup_expired()

        candidates: list[GenerationHandle] = []
        with self._lock:
            if key in self._handles:
                raise ValueError(f"request_id already exists: {key}")

            if source_key:
                source = self._handles.get(source_key)
                if source is not None and source.session_id != session_key:
                    raise CrossSessionPreemptionError(
                        "source_request_id belongs to another session"
                    )
                if source is not None:
                    candidates.append(source)

            active_request_id = self._active_by_session.get(session_key)
            active = self._handles.get(active_request_id or "")
            if active is not None and all(active is not item for item in candidates):
                candidates.append(active)

            handle = GenerationHandle(request_id=key, session_id=session_key)
            self._handles[key] = handle
            self._active_by_session[session_key] = key

        preempted: list[GenerationHandle] = []
        for previous in candidates:
            if previous.terminal:
                continue
            # 不 join closer：抢占常在 asyncio 事件循环线程调用，join 会拖死整站
            previous.request_cancel("preempted", join_closers=False)
            preempted.append(previous)
        return RegistrationResult(handle=handle, preempted_handles=tuple(preempted))

    def register(self, request_id: str, session_id: str) -> GenerationHandle:
        """Backward-compatible registration with same-session preemption enabled."""
        return self.register_or_preempt(request_id, session_id).handle

    def get(self, request_id: str) -> Optional[GenerationHandle]:
        with self._lock:
            return self._handles.get((request_id or "").strip())

    def active_request_id(self, session_id: str) -> str:
        with self._lock:
            return self._active_by_session.get((session_id or "").strip(), "")

    def resolve_for_stop(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> Optional[GenerationHandle]:
        """
        解析要停止的生成句柄。
        - 产品主路径：只传 session_id → 停该会话当前活跃轮
        - 内部/兼容：可传 request_id 精确指定；若同时传 session_id 则校验归属
        """
        rid = (request_id or "").strip()
        sid = (session_id or "").strip()
        if rid:
            handle = self.get(rid)
            if handle is None:
                return None
            if sid and handle.session_id != sid:
                raise CrossSessionPreemptionError(
                    "session_id 与 request_id 不匹配"
                )
            return handle
        if not sid:
            raise ValueError("session_id 不能为空")
        active = self.active_request_id(sid)
        if not active:
            return None
        return self.get(active)

    def request_cancel(self, request_id: str, reason: str = "user") -> CancelResult:
        key = (request_id or "").strip()
        handle = self.get(key)
        if handle is None:
            return CancelResult(request_id=key)
        accepted = handle.request_cancel(reason)
        return CancelResult(
            request_id=key,
            session_id=handle.session_id,
            cancelled=accepted,
            status=handle.status,
        )

    def wait_terminal(self, request_id: str, timeout: Optional[float] = None) -> bool:
        handle = self.get(request_id)
        return True if handle is None else handle.wait_terminal(timeout)

    def finish(self, request_id: str, status: str) -> None:
        handle = self.get(request_id)
        if handle is None:
            return
        handle.finish(status)
        with self._lock:
            if self._active_by_session.get(handle.session_id) == handle.request_id:
                self._active_by_session.pop(handle.session_id, None)

    def cleanup_expired(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        with self._lock:
            expired = [
                key
                for key, handle in self._handles.items()
                if handle.updated_at < cutoff
            ]
            for key in expired:
                handle = self._handles.pop(key, None)
                if (
                    handle is not None
                    and self._active_by_session.get(handle.session_id) == key
                ):
                    self._active_by_session.pop(handle.session_id, None)


_registry = CancellationRegistry()


def get_cancellation_registry() -> CancellationRegistry:
    return _registry
