"""
辅助决策前端 SSE 协议适配器。

将 BettaFish 内部分析事件映射为前端约定的两阶段流式格式（类 DeepSeek 深度思考）：
  - event: thinking  — 进度 + 章节 CoT（可折叠）
  - event: result    — 最终 Markdown 正文流式输出
  - : ping           — 心跳

参考样例：辅助决策流式输出内容实例.txt
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_REPORT_INDEX = 1
DEFAULT_CHUNK_SIZE = 12


def format_decision_sse(channel: str, data: Dict[str, Any]) -> str:
    """格式化为前端 decision SSE 行。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {channel}\ndata: {payload}\n\n"


def format_decision_ping() -> str:
    return ": ping\n\n"


def iter_text_chunks(text: str, size: int = DEFAULT_CHUNK_SIZE) -> Iterable[str]:
    """将 Markdown 文本切分为小块，模拟 token 流。"""
    if not text:
        return
    step = max(1, int(size))
    for i in range(0, len(text), step):
        yield text[i : i + step]


@dataclass
class DecisionStreamContext:
    """单次任务的 decision 流状态。"""
    title: str = "分析报告"
    report_index: int = DEFAULT_REPORT_INDEX
    current_section: str = "report"
    thinking_closed: bool = False
    result_started: bool = False


class DecisionSSETranslator:
    """内部分析事件 → decision SSE 文本行。"""

    def __init__(self, ctx: Optional[DecisionStreamContext] = None):
        self.ctx = ctx or DecisionStreamContext()

    def translate(self, event: Dict[str, Any]) -> List[str]:
        etype = event.get("type") or ""
        payload = event.get("payload") or {}

        if etype == "task_started":
            return [
                format_decision_sse(
                    "thinking",
                    {"type": "progress", "message": "收到请求，正在启动分析任务…"},
                )
            ]

        if etype in ("workflow", "engine_progress", "task_progress"):
            message = (payload.get("message") or "").strip()
            if not message:
                return []
            data: Dict[str, Any] = {"type": "progress", "message": message}
            progress = payload.get("progress")
            if isinstance(progress, (int, float)):
                data["progress"] = int(progress)
            return [format_decision_sse("thinking", data)]

        if etype == "agent_log":
            message = (payload.get("message") or payload.get("text") or "").strip()
            if not message:
                return []
            return [
                format_decision_sse(
                    "thinking",
                    {"type": "progress", "message": message[:500]},
                )
            ]

        if etype == "report_stage":
            return self._translate_report_stage(payload)

        if etype == "task_completed":
            return self._translate_task_completed(payload)

        if etype == "task_error":
            message = payload.get("error_message") or payload.get("error_reason") or "任务失败"
            return [
                format_decision_sse(
                    "thinking",
                    {"type": "done", "success": False, "message": message},
                )
            ]

        return []

    def _translate_report_stage(self, payload: Dict[str, Any]) -> List[str]:
        report_event = payload.get("report_event") or ""
        lines: List[str] = []

        if report_event == "chapter_status":
            title = payload.get("title") or "章节"
            status = payload.get("status")
            if status == "running":
                self.ctx.current_section = title
                lines.append(
                    format_decision_sse(
                        "thinking",
                        {
                            "type": "section_start",
                            "index": self.ctx.report_index,
                            "stage": "report",
                            "title": title,
                        },
                    )
                )
            elif status in ("completed", "done", "success"):
                lines.append(
                    format_decision_sse(
                        "thinking",
                        {
                            "type": "section_end",
                            "index": self.ctx.report_index,
                            "stage": "report",
                        },
                    )
                )
                lines.append(
                    format_decision_sse(
                        "thinking",
                        {
                            "type": "section_fold",
                            "index": self.ctx.report_index,
                            "stage": "report",
                        },
                    )
                )
            return lines

        if report_event == "chapter_chunk":
            delta = payload.get("delta") or ""
            if not delta:
                return []
            return [
                format_decision_sse(
                    "thinking",
                    {
                        "index": self.ctx.report_index,
                        "stage": self.ctx.current_section or "report",
                        "type": "chunk",
                        "text": delta,
                    },
                )
            ]

        stage = payload.get("stage") or ""

        if stage == "markdown_stream_start":
            if not self.ctx.thinking_closed:
                lines.append(
                    format_decision_sse(
                        "thinking",
                        {"type": "done", "message": "思考结束"},
                    )
                )
                self.ctx.thinking_closed = True
            if not self.ctx.result_started:
                lines.append(
                    format_decision_sse(
                        "result",
                        {
                            "type": "start",
                            "index": self.ctx.report_index,
                            "title": self.ctx.title,
                            "file_path": payload.get("markdown_file") or "",
                        },
                    )
                )
                self.ctx.result_started = True
            return lines

        if stage == "markdown_stream_chunk":
            text = payload.get("text") or ""
            if not text:
                return []
            if not self.ctx.result_started:
                lines.extend(self._translate_report_stage({
                    "report_event": "stage",
                    "stage": "markdown_stream_start",
                    "markdown_file": payload.get("markdown_file") or "",
                }))
            return lines + [
                format_decision_sse(
                    "result",
                    {
                        "index": self.ctx.report_index,
                        "type": "chunk",
                        "text": text,
                    },
                )
            ]

        if stage == "markdown_stream_end":
            lines.append(
                format_decision_sse(
                    "result",
                    {
                        "type": "end",
                        "index": self.ctx.report_index,
                        "file_path": payload.get("markdown_file") or "",
                    },
                )
            )
            return lines

        message = payload.get("message") or ""
        if message and report_event in ("stage", "progress"):
            return [
                format_decision_sse(
                    "thinking",
                    {"type": "progress", "message": f"报告引擎：{message}"},
                )
            ]
        return lines

    def _translate_task_completed(self, payload: Dict[str, Any]) -> List[str]:
        markdown_file = payload.get("markdown_file") or ""
        reports = [{"index": self.ctx.report_index, "file_path": markdown_file}]
        lines: List[str] = []
        if not self.ctx.thinking_closed:
            lines.append(
                format_decision_sse(
                    "thinking",
                    {"type": "done", "message": "思考结束"},
                )
            )
            self.ctx.thinking_closed = True
        lines.append(
            format_decision_sse(
                "thinking",
                {
                    "type": "done",
                    "success": True,
                    "reports": reports,
                    "markdown_ready": payload.get("markdown_ready", False),
                    "markdown_url": payload.get("markdown_url"),
                },
            )
        )
        lines.append(format_decision_sse("reference", {"type": "done"}))
        return lines


def build_markdown_stream_events(
    markdown_text: str,
    markdown_file: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    title: str = "分析报告",
) -> List[Dict[str, Any]]:
    """构造 Markdown 流式 report_stage 事件列表（供任务层 emit）。"""
    events: List[Dict[str, Any]] = [
        {
            "report_event": "stage",
            "stage": "markdown_stream_start",
            "markdown_file": markdown_file,
            "title": title,
        }
    ]
    for chunk in iter_text_chunks(markdown_text, chunk_size):
        events.append({
            "report_event": "stage",
            "stage": "markdown_stream_chunk",
            "text": chunk,
            "markdown_file": markdown_file,
        })
    events.append({
        "report_event": "stage",
        "stage": "markdown_stream_end",
        "markdown_file": markdown_file,
    })
    return events
