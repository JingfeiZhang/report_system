"""
分析任务 workflow 事件：将内部进度映射为人类可读文案，并过滤 report_stage 的 token delta。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

ENGINE_LABELS = {
    "insight": "舆情引擎",
    "media": "媒体引擎",
    "query": "查询引擎",
    "report": "报告引擎",
    "forum": "协同讨论",
    "system": "系统",
}

_REPORT_DELTA_KEYS = frozenset({"delta"})


def engine_label(engine: str) -> str:
    return ENGINE_LABELS.get(engine or "", engine or "系统")


def build_workflow_payload(
    phase: str,
    step: str,
    message: str,
    *,
    level: str = "info",
    progress_hint: Optional[int] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "phase": phase,
        "step": step,
        "level": level,
        "message": message,
    }
    if progress_hint is not None:
        payload["progress_hint"] = int(max(0, min(100, progress_hint)))
    if detail:
        payload["detail"] = detail
    return payload


def workflow_from_engine_progress(
    engine: str, progress: int, message: str
) -> Optional[Dict[str, Any]]:
    label = engine_label(engine)
    text = (message or "").strip()
    if not text:
        return None

    step = "start"
    level = "info"
    lowered = text.lower()
    if "搜索" in text or "检索" in text or "search" in lowered:
        step = "search"
    elif "总结" in text or "反思" in text or "summary" in lowered:
        step = "summary"
    elif "结构" in text or "段落" in text or "规划" in text:
        step = "structure"
    elif "报告" in text or "章节" in text or "chapter" in lowered:
        step = "chapter"
    elif "完成" in text:
        step = "done"
        level = "success"

    if engine == "report":
        phase = "report"
    elif engine == "forum":
        phase = "forum"
    else:
        phase = engine if engine in ENGINE_LABELS else "system"

    display = text if text.startswith(label) else f"{label}：{text}"
    return build_workflow_payload(
        phase, step, display, level=level if step == "done" else "info", progress_hint=progress
    )


def workflow_from_task_message(message: str, progress: int) -> Optional[Dict[str, Any]]:
    text = (message or "").strip()
    if not text:
        return None
    step = "start"
    if "Report" in text or "报告" in text:
        phase = "report"
    elif "引擎" in text:
        phase = "system"
    else:
        phase = "system"
    return build_workflow_payload(phase, step, text, progress_hint=progress)


def workflow_from_report_event(
    report_event: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if report_event in ("stage", "progress"):
        stage = payload.get("stage") or ""
        message = payload.get("message") or ""
        if stage == "template_selected":
            name = payload.get("template") or "默认模板"
            return build_workflow_payload(
                "report",
                "template",
                f"报告引擎：已选择版式模板「{name}」",
            )
        if stage == "layout_designed":
            title = payload.get("title") or payload.get("toc") or ""
            return build_workflow_payload(
                "report",
                "template",
                f"报告引擎：完成标题与目录设计{f'（{title}）' if title else ''}",
            )
        if stage == "markdown_generated":
            return build_workflow_payload(
                "report", "export", "报告引擎：Markdown 已生成", level="success"
            )
        if stage == "markdown_failed":
            return build_workflow_payload(
                "report",
                "export",
                f"报告引擎：Markdown 生成失败（{payload.get('message') or '未知错误'}）",
                level="error",
            )
        if stage == "pdf_generating":
            return build_workflow_payload("report", "export", "报告引擎：正在生成 PDF…")
        if stage == "pdf_generated":
            return build_workflow_payload(
                "report", "export", "报告引擎：PDF 已生成", level="success"
            )
        if stage == "pdf_skipped":
            return build_workflow_payload(
                "report", "export", "报告引擎：已跳过 PDF 导出", level="warning"
            )
        if message:
            return build_workflow_payload("report", "chapter", f"报告引擎：{message}")
        return None

    if report_event == "chapter_status":
        title = payload.get("title") or "章节"
        status = payload.get("status")
        if status == "running":
            return build_workflow_payload(
                "report",
                "chapter",
                f"报告引擎：正在撰写「{title}」…",
                detail={"chapter_title": title},
            )
        if status in ("completed", "done", "success"):
            return build_workflow_payload(
                "report",
                "chapter",
                f"报告引擎：章节「{title}」已完成",
                level="success",
                detail={"chapter_title": title},
            )
        if status == "error":
            return build_workflow_payload(
                "report",
                "chapter",
                f"报告引擎：章节「{title}」生成异常",
                level="error",
                detail={"chapter_title": title},
            )
        return None

    if report_event == "chapter_chunk":
        return None

    return None


def should_emit_report_stage(payload: Dict[str, Any], verbose: bool) -> bool:
    """非 verbose 模式下抑制逐 token 的 chapter_chunk / delta。"""
    if verbose:
        return True
    report_event = payload.get("report_event") or ""
    if report_event == "chapter_chunk":
        return False
    if payload.get("delta"):
        return False
    if report_event in ("stage", "progress", "chapter_status"):
        return False
    return True


def scrub_report_stage_payload(payload: Dict[str, Any], verbose: bool) -> Dict[str, Any]:
    if verbose:
        return payload
    cleaned = dict(payload)
    cleaned.pop("delta", None)
    return cleaned
