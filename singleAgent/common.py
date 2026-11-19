"""singleAgent 公共工具。"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Generator

from singleAgent.config import (
    THINKING_CHUNK_DELAY_SEC,
    THINKING_CHUNK_SIZE,
    THINKING_PROGRESS_DELAY_SEC,
    THINKING_SUMMARY_MAX_CHUNKS,
)
from singleAgent.sse import emit_thinking_chunk, emit_thinking_progress


def pace(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def yield_progress(message: str, *, delay: float | None = None) -> Generator[str, None, None]:
    pace(delay if delay is not None else THINKING_PROGRESS_DELAY_SEC)
    yield emit_thinking_progress(message)


def yield_thinking_chunks(
    text: str,
    *,
    stage: str = "search",
) -> Generator[str, None, None]:
    size = max(THINKING_CHUNK_SIZE, 8)
    for i in range(0, len(text), size):
        yield emit_thinking_chunk(text[i : i + size], stage=stage)
        pace(THINKING_CHUNK_DELAY_SEC)


def yield_thinking_summary(
    text: str,
    *,
    stage: str = "search",
    max_chunks: int | None = None,
) -> Generator[str, None, None]:
    """检索摘要等长文本：仍发 ``chunk``（挂在 section「检索摘要」下），按行切包。

    注意：不可改成 ``progress``——正式前端只把 ``chunk`` 归入 section 正文，
    progress 会冒到上半思维链，导致「检索摘要」标题下空白。
    ``max_chunks`` 只控制 SSE 包数量（多行可合并进一包），**不丢弃**正文行。
    """
    content = (text or "").strip()
    if not content:
        return
    lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return
    cap = max_chunks if max_chunks is not None else THINKING_SUMMARY_MAX_CHUNKS
    cap = max(1, int(cap))
    # 行数不多则一行一包；过长则均分合并，保证全文仍下发
    if len(lines) <= cap:
        batches = [[ln] for ln in lines]
    else:
        batch_size = max(1, (len(lines) + cap - 1) // cap)
        batches = [
            lines[i : i + batch_size] for i in range(0, len(lines), batch_size)
        ]
    for i, batch in enumerate(batches):
        piece = "\n".join(batch)
        if i + 1 < len(batches):
            piece += "\n"
        yield emit_thinking_chunk(piece, stage=stage)
        if i + 1 < len(batches):
            pace(THINKING_CHUNK_DELAY_SEC)


def safe_filename(topic: str) -> str:
    text = "".join(c for c in topic if c.isalnum() or c in (" ", "-", "_")).strip()
    return (text.replace(" ", "_")[:30] or "report")


def save_markdown(topic: str, content: str, project_root: Path) -> str:
    out_dir = project_root / "final_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"fast_{safe_filename(topic)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    path = out_dir / name
    path.write_text(content, encoding="utf-8")
    try:
        return os.path.relpath(str(path.resolve()), os.getcwd())
    except ValueError:
        return str(path)
