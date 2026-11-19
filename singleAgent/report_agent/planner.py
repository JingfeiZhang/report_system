"""ReportPlannerAgent：模板 + EventBrief → SectionPlan[]。"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from loguru import logger

from singleAgent.llm import complete_chat
from singleAgent.report_agent.models import InformationNeed, SectionPlan
from singleAgent.report_agent.prompts import PLANNER_SYSTEM, build_planner_user_prompt
from singleAgent.report_agent.models import EventBrief
from singleAgent.report_template_loader import ReportTemplate


def _agent_int(name: str, default: int) -> int:
    return max(1, int(os.getenv(name, str(default))))


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_planner_json(raw: str) -> Optional[List[SectionPlan]]:
    text = _strip_fence(raw)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    sections_raw = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(sections_raw, list) or not sections_raw:
        return None
    sections = [
        SectionPlan.from_dict(item, index=i + 1)
        for i, item in enumerate(sections_raw)
        if isinstance(item, dict) and item.get("title")
    ]
    return sections or None


def _fallback_sections(template: ReportTemplate, event_brief: EventBrief) -> List[SectionPlan]:
    """LLM 失败时的默认 3 节（对齐 situation_brief 等值班简报）。"""
    entities = "、".join(event_brief.entities[:6]) if event_brief.entities else "相关部队"
    locs = "、".join(event_brief.locations[:4]) if event_brief.locations else "相关地域"
    kw_base = list(dict.fromkeys(
        (event_brief.event_types or []) + ["部署", "机动", "部队", "日志"]
    ))[:6]

    def _need(nid: str, desc: str, extra_kw: List[str]) -> InformationNeed:
        return InformationNeed(
            id=nid,
            description=desc,
            keywords=list(dict.fromkeys(extra_kw + kw_base))[:8],
            suggested_tables_hint=["战斗日志", "部队编制", "部署记录"],
            priority="required",
        )

    gen = (template.generation_content or "").lower()
    if "核心态势" in gen or "situation" in template.id:
        titles = [
            ("s1_overview", "核心态势", "用一句话概括当前事件状态与结论"),
            ("s2_moves", "主要动向", "列出 2～4 条可核验的最新变化"),
            ("s3_watch", "关注事项", "列出下一步需观察的信号或风险"),
        ]
    else:
        titles = [
            ("s1_overview", "态势综述", "概述事件背景与当前状态"),
            ("s2_evidence", "关键事实", "列出可核验的时间线、兵力与地点"),
            ("s3_assessment", "研判与关注", "基于材料给出关注点与信息缺口"),
        ]

    return [
        SectionPlan(
            section_id=sid,
            title=title,
            guidance=guide,
            needs=[
                _need(
                    f"{sid}_n1",
                    f"查询与「{title}」相关的{entities}在{locs}的库内记录",
                    [title[:4], entities[:20] if entities else "部队"],
                )
            ],
            status="pending",
        )
        for sid, title, guide in titles[: _agent_int("REPORT_AGENT_MAX_SECTIONS", 8)]
    ]


def plan_report_sections(
    *,
    event_brief: EventBrief,
    template: ReportTemplate,
    topic: str,
    llm_cfg: Optional[dict],
) -> tuple[List[SectionPlan], str]:
    """返回 (sections, source) source=llm|fallback。"""
    if not llm_cfg or not llm_cfg.get("base_url"):
        return _fallback_sections(template, event_brief), "fallback"

    try:
        raw = complete_chat(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            model_name=llm_cfg["model_name"],
            system_prompt=PLANNER_SYSTEM,
            user_prompt=build_planner_user_prompt(
                event_brief=event_brief,
                template=template,
                topic=topic,
            ),
            max_tokens=_agent_int("REPORT_AGENT_PLAN_LLM_MAX_TOKENS", 1536),
            temperature=0,
            timeout=float(os.getenv("REPORT_AGENT_PLAN_LLM_TIMEOUT_SEC", "60")),
            check_connectivity=False,
        )
        sections = _parse_planner_json(raw)
        if sections:
            max_sec = _agent_int("REPORT_AGENT_MAX_SECTIONS", 8)
            return sections[:max_sec], "llm"
        logger.warning("ReportPlanner JSON 解析失败，使用 fallback")
    except Exception as exc:
        logger.warning("ReportPlanner LLM 失败: {}", exc)

    return _fallback_sections(template, event_brief), "fallback"
