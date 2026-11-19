"""singleAgent 直接输出 decision SSE 行。"""

from __future__ import annotations

import json
from typing import Any, Dict

from decision_sse import format_decision_ping, format_decision_sse

from singleAgent.config import REPORT_INDEX


def attach_event_identity(frame: str, *, request_id: str, session_id: str) -> str:
    """Add request/session identity to every JSON data frame in an event2 SSE chunk."""
    if not frame or (not request_id and not session_id):
        return frame

    trailing_separator = frame.endswith("\n\n")
    blocks = frame.split("\n\n")
    if trailing_separator and blocks and blocks[-1] == "":
        blocks.pop()
    updated_blocks: list[str] = []
    for block in blocks:
        lines = block.splitlines()
        updated_lines: list[str] = []
        for line in lines:
            if not line.startswith("data:"):
                updated_lines.append(line)
                continue
            raw_data = line[5:].strip()
            try:
                data = json.loads(raw_data)
            except (TypeError, ValueError):
                updated_lines.append(line)
                continue
            if not isinstance(data, dict):
                updated_lines.append(line)
                continue
            if request_id:
                data["request_id"] = request_id
            if session_id:
                data["session_id"] = session_id
            updated_lines.append(
                f"data: {json.dumps(data, ensure_ascii=False)}"
            )
        updated_blocks.append("\n".join(updated_lines))
    suffix = "\n\n" if trailing_separator else ""
    return "\n\n".join(updated_blocks) + suffix


def emit_thinking_progress(message: str, **extra: Any) -> str:
    data: Dict[str, Any] = {"type": "progress", "message": message}
    data.update(extra)
    return format_decision_sse("thinking", data)


def emit_thinking_done(message: str = "思考结束", **extra: Any) -> str:
    data: Dict[str, Any] = {"type": "done", "message": message}
    data.update(extra)
    return format_decision_sse("thinking", data)


def emit_thinking_section_start(title: str, stage: str = "search") -> str:
    return format_decision_sse(
        "thinking",
        {
            "type": "section_start",
            "index": REPORT_INDEX,
            "stage": stage,
            "title": title,
        },
    )


def emit_thinking_chunk(text: str, stage: str = "search") -> str:
    return format_decision_sse(
        "thinking",
        {
            "index": REPORT_INDEX,
            "stage": stage,
            "type": "chunk",
            "text": text,
        },
    )


def emit_thinking_section_end(stage: str = "search") -> str:
    return format_decision_sse(
        "thinking",
        {
            "type": "section_end",
            "index": REPORT_INDEX,
            "stage": stage,
        },
    )


def emit_result_start(title: str, file_path: str = "") -> str:
    return format_decision_sse(
        "result",
        {
            "type": "start",
            "index": REPORT_INDEX,
            "title": title,
            "file_path": file_path,
        },
    )


def emit_result_chunk(text: str) -> str:
    return emit_result_typed(text, "chunk")


def emit_result_typed(
    text: str,
    chunk_type: str = "chunk",
    aux_kind: str | None = None,
) -> str:
    """
    result 流式块。
    - chunk: 可编辑正文
    - aux: 附属说明（小结/结语/备注/免责声明等），可选 aux_kind
    - awaiting_template: 等用户勾选右侧模板时的短提示（非报告正文）
    """
    data: Dict[str, Any] = {"index": REPORT_INDEX, "type": chunk_type, "text": text}
    if chunk_type == "aux":
        data["aux_kind"] = aux_kind or "unknown"
    return format_decision_sse("result", data)


def emit_result_end(file_path: str = "") -> str:
    return format_decision_sse(
        "result",
        {"type": "end", "index": REPORT_INDEX, "file_path": file_path},
    )


def emit_reference_done() -> str:
    return format_decision_sse("reference", {"type": "done"})


def emit_cancelled(*, request_id: str, session_id: str, reason: str = "user") -> str:
    return format_decision_sse(
        "cancelled",
        {
            "request_id": request_id,
            "session_id": session_id,
            "reason": reason or "user",
        },
    )


def emit_retrieval_knowledge(
    *,
    refs: list[Dict[str, Any]],
    message: str,
    session_id: str = "",
    progress: int = 0,
    retrieval_type: str = "knowledge",
) -> str:
    """右栏 retrieval 帧。

    - 报告模板：``retrieval_type="knowledge"``（前端多模块约定只认此 type 解析模板）
    - 知识库命中：``retrieval_type="general_knowledge"``（勿与模板共用 knowledge）
    """
    data: Dict[str, Any] = {
        "type": retrieval_type or "knowledge",
        "message": message,
        "refs": refs,
        "progress": progress,
    }
    if session_id:
        data["session_id"] = session_id
    return format_decision_sse("retrieval", data)


def emit_tip(
    *,
    label: str,
    block_id: str,
    dataset_name: str,
    document_name: str,
    text: str,
    index: int = 0,
    published_at: str = "",
) -> str:
    """正文溯源 tip（悬停弹层）。

    对齐前端契约：``event: tip`` + ``delta.label`` 对应正文 ``(标N)``。
    ``published_at`` 可选；无日期时省略该字段。
    """
    delta: Dict[str, Any] = {
        "type": "text_delta",
        "label": label,
        "block_id": block_id,
        "dataset_name": dataset_name,
        "document_name": document_name,
        "text": text,
    }
    if published_at:
        delta["published_at"] = published_at
    return format_decision_sse(
        "tip",
        {"type": "tip", "index": index, "delta": delta},
    )


def emit_ping() -> str:
    return format_decision_ping()
