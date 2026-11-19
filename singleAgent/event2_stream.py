"""event2 流式运行时：独立线程池，避免占满 anyio 默认 to_thread 池拖死整站。

Starlette 对 **同步** StreamingResponse 迭代器走 ``iterate_in_threadpool``（anyio
CapacityLimiter，默认约 40）。客户端中途断开不会打断线程里正在跑的同步调用，
槽位要等生成器跑到下一个 yield 才释放。多次「新对话」会留下僵尸流；若同时
``await asyncio.to_thread(前置检查)`` 也抢同一池，事件循环会在拿槽位时饿死，
表现为整站「罢工」（/health 超时、新请求卡住）。

本模块：
- 前置检查 / stop 等短任务：``EVENT2_PREFLIGHT`` 专用池
- SSE 本体：``EVENT2_STREAM`` 专用池 + asyncio.Queue 桥接成 **异步** 迭代器
  （不再占用 anyio 默认池）
- 并发槽在客户端断开时立即释放，旧 worker 可在后台排空，不堵新对话
"""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Iterator, Optional, TypeVar

from loguru import logger

T = TypeVar("T")

_PREFLIGHT_WORKERS = max(
    2, int(os.getenv("EVENT2_PREFLIGHT_WORKERS", "4") or "4")
)
_STREAM_WORKERS = max(
    2, int(os.getenv("EVENT2_STREAM_MAX_WORKERS", "12") or "12")
)
_MAX_CONCURRENT = max(
    1, int(os.getenv("EVENT2_MAX_CONCURRENT_STREAMS", "8") or "8")
)
_ACQUIRE_TIMEOUT = float(
    os.getenv("EVENT2_STREAM_ACQUIRE_TIMEOUT_SEC", "2") or "2"
)

_PREFLIGHT_EXECUTOR = ThreadPoolExecutor(
    max_workers=_PREFLIGHT_WORKERS,
    thread_name_prefix="event2-pre",
)
_STREAM_EXECUTOR = ThreadPoolExecutor(
    max_workers=_STREAM_WORKERS,
    thread_name_prefix="event2-stream",
)
_stream_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)


class Event2StreamBusy(Exception):
    """并发流已满，调用方应返回 503。"""


def try_acquire_event2_stream_slot() -> None:
    """在返回 StreamingResponse 之前占用并发槽；失败抛 Event2StreamBusy。"""
    if not _stream_slots.acquire(timeout=_ACQUIRE_TIMEOUT):
        raise Event2StreamBusy(
            f"event2 并发流已满（max={_MAX_CONCURRENT}），请稍后重试"
        )


def release_event2_stream_slot() -> None:
    try:
        _stream_slots.release()
    except ValueError:
        pass


async def run_in_event2_executor(
    fn: Callable[..., T],
    /,
    *args: Any,
    timeout: float = 5.0,
    **kwargs: Any,
) -> T:
    """在专用前置池执行同步函数，带超时，不占用 anyio 默认 to_thread 池。"""
    loop = asyncio.get_running_loop()

    def _call() -> T:
        return fn(*args, **kwargs)

    fut = loop.run_in_executor(_PREFLIGHT_EXECUTOR, _call)
    return await asyncio.wait_for(fut, timeout=timeout)


async def iter_sync_sse(
    factory: Callable[[], Iterator[str]],
    *,
    label: str = "event2",
    slot_acquired: bool = False,
) -> AsyncIterator[str]:
    """把同步 SSE 生成器放到专用线程，经 Queue 异步 yield。

    客户端断开时：停止取帧、置 stop，并对仍停在 yield 处的生成器 ``close()``，
    触发现有 disconnect/cancel 路径。达梦等阻塞调用仍靠 cancellation 词间检查退出。

    slot_acquired=True：调用方已 ``try_acquire_event2_stream_slot()``（推荐，可在
    建连前返回 503）。槽位在本异步迭代结束时释放，不要求 worker 排空完毕。
    """
    owns_slot = False
    if slot_acquired:
        owns_slot = True
    else:
        try_acquire_event2_stream_slot()
        owns_slot = True

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    stop = threading.Event()

    def _put(msg: tuple) -> None:
        if stop.is_set() and msg[0] == "ok":
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(queue.put(msg), loop)
            while True:
                if stop.is_set() and msg[0] == "ok":
                    fut.cancel()
                    return
                try:
                    fut.result(timeout=0.25)
                    return
                except TimeoutError:
                    continue
        except Exception as exc:
            logger.debug(f"event2 stream queue put failed label={label}: {exc}")

    def worker() -> None:
        gen: Optional[Iterator[str]] = None
        try:
            gen = factory()
            for frame in gen:
                if stop.is_set():
                    break
                _put(("ok", frame))
                if stop.is_set():
                    break
            _put(("end", None))
        except BaseException as exc:
            if isinstance(exc, GeneratorExit):
                _put(("end", None))
                raise
            _put(("err", exc))
        finally:
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass

    _STREAM_EXECUTOR.submit(worker)
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "end":
                break
            if kind == "err":
                raise payload
            yield payload
    finally:
        stop.set()
        if owns_slot:
            release_event2_stream_slot()
