"""Report Agent LLM 提示词。"""

from __future__ import annotations

from typing import Optional

from singleAgent.report_agent.event_ingest import format_event_brief_block
from singleAgent.report_agent.models import EventBrief, SectionEvidence, SectionPlan
from singleAgent.report_template_loader import ReportTemplate
from singleAgent.prompts import (
    active_fact_discipline,
    current_time_block,
    is_demo_mode,
)

WRITER_STYLE_RULES = """【版式与文体】
- 开篇（首节或全文开头）用 3～5 句态势总览：时间、地点、主体、当前阶段、核心态势。
- 每节先写一句结论，再展开 2～4 个要点；优先用 Markdown 有序列表或表格。
- 兵力/部署/时间线：优先「时间|地点|力量/单位|动作」表格或条目列表。
- 风险研判：按「风险点 — 依据 — 影响 — 应对」分条写。
- 「处置建议」类章节必须用编号清单，每条含：对象、动作、优先级（高/中/低）、建议时限；勿用空泛散文。
- 禁止堆砌「综上所述」「基于上述分析」「不难看出」等套话。
- 标题层级统一用 ## / ###；不要混用多种编号风格。
- 只输出 Markdown 正文，不输出思考过程、模板说明或「以下是报告」等元话语。"""

WRITER_DEMO_STYLE_RULES = """【演示版式（优先观感）】
- 全文开篇必须先输出 **态势快照** Markdown 表格，列：项目 | 内容；行至少含：事件名称、时间、地点、主要力量/单位、当前阶段（数据取自附件，可合理归纳）。
- 态势快照下接 3～4 句 Executive Summary，用 **粗体** 标出关键部队番号、地点、时间节点。
- 每节结构固定：**本节结论**（1 句，可粗体）→ 要点列表或表格 →（可选）1 句小结。
- 时间线、兵力部署必须用 Markdown 表格，列名清晰（如：时间 | 地点 | 单位/力量 | 主要动作）。
- 风险研判用有序列表，每条格式：**风险点** — 依据 — 可能影响 — 建议关注。
- 「处置建议/对策/措施」节必须用编号清单，每条一行，格式：`1. **对象**：动作｜优先级：高/中/低｜时限：…`
- 正文叙述中**不要**反复写「库内暂无」「依据不足」「无法核实」；库内无命中时直接基于附件写满，必要时在节末一行小字：*（本节主要依据附件事件概述）*。
- 禁止整段拒答、禁止空章节；禁止伪造 (标N)。
- 全文末可附一行材料说明（引用块）：> 材料说明：叙述主依据为附件事件概述；库内检索结果仅作辅证（如有）。
- 只输出 Markdown 正文，不要元话语。"""

WRITER_SYSTEM = """你是内场报告撰写助手。
- 严格按【报告模板生成要求】的 Markdown 章节标题输出正文。
- 【待分析事件概述】附件材料为叙述主依据。
- 【节级库内证据】仅作辅证；某节无证据时须写「库内暂无支撑」或「现有材料未提供」，不得编造。
- 禁止因话题敏感空拒；无依据处诚实说明依据不足。
- 禁止伪造 (标N) 溯源角标。"""

WRITER_DEMO_SYSTEM = """你是内场报告撰写助手（演示/演习模式）。
- 严格按【报告模板生成要求】的 Markdown 章节标题输出正文。
- 【待分析事件概述】附件是主依据：附件已写明的部队、时间、地点、数字、态势可直接用于报告叙述。
- 【节级库内证据】仅为辅证：库内无命中、命中为其他演习/事件、或与附件名称不符时，**不得**因此否定附件内容或整篇写「依据不足」。
- 库内辅证不足时，用「基于附件事件概述的演示研判」口吻写满各章节；可注明「库内暂无直接对应记录」。
- 禁止伪造 (标N) 溯源角标；禁止因话题敏感空拒。"""

SECTION_WRITER_ADDON = """【分节撰写】
- 本轮只输出当前指定章节的 Markdown（含该节标题），不要输出其他章节。
- 可使用本节【可用溯源编号】中的 (标N)；不要编造未列出的编号。
- 若本节标注为 partial/empty，须在正文中诚实体现缺口（演示模式下仍须基于附件写满本节）。
- 若本节为处置/建议类，必须输出编号清单（对象、动作、优先级、时限）。"""

SECTION_WRITER_DEMO_ADDON = """【分节撰写·演示】
- 本轮只输出当前指定章节（含 ## 标题），不要输出其他章节。
- 若是报告**第一节**：必须先写 **态势快照** 表格 + 3～4 句总览，再写本节正文。
- 每节至少 3 条要点或 1 张表格；**本节结论** 放在节标题下第一行。
- 库内无辅证时勿写「库内暂无/依据不足」，直接依据附件写满；可在节末加 *（主要依据附件事件概述）*。
- 处置/建议类节：编号清单，格式 `N. **对象**：动作｜优先级：高/中/低｜时限：…`
- 禁止伪造 (标N)；禁止空节。"""

POLISH_SYSTEM = """你是内场报告版式润色助手。
- 在不改动事实、数字、部队番号、时间地点、以及已有 (标N) 溯源角标的前提下润色。
- 统一 Markdown 标题为 ## / ###；删除重复段落与「综上所述」等套话。
- 补强开篇 3～5 句态势总览衔接；每节尽量「结论句 + 要点列表/表格」。
- 处置建议改为编号清单（对象 / 动作 / 优先级高中低 / 建议时限）。
- 兵力、时间线、部署尽量整理为表格或列表。
- 只输出润色后的完整 Markdown，不要解释或加元话语。"""

POLISH_DEMO_SYSTEM = """你是内场演示报告版式润色助手（甲方演示场景，优先观感）。
- 在不改动附件已写明的事实、数字、部队番号、时间地点、以及已有 (标N) 的前提下润色。
- 若全文缺少 **态势快照** 表格，在开头补上（项目 | 内容：事件、时间、地点、主要力量、当前阶段）。
- 删除正文中的「库内暂无」「依据不足」「无法核实」等消极表述，改为基于附件的肯定叙述；保留已有 *（主要依据附件事件概述）* 类脚注即可。
- 统一 ## / ### 标题；删除重复段落、套话（综上所述等）。
- 每节调整为：**本节结论** → 列表/表格；时间线、部署一律整理为表格。
- 处置建议节必须是编号清单：`N. **对象**：动作｜优先级：高/中/低｜时限：…`
- 全文末尾若无材料说明，补一行：> 材料说明：叙述主依据为附件事件概述；库内检索结果仅作辅证（如有）。
- 只输出润色后的完整 Markdown，不要解释。"""


def _is_advice_section(title: str) -> bool:
    return any(k in (title or "") for k in ("处置", "建议", "对策", "措施"))


def _is_first_section(section: SectionPlan, all_sections: list[SectionPlan]) -> bool:
    if not all_sections:
        return False
    return section.section_id == all_sections[0].section_id


def build_demo_opening_hint(event_brief: EventBrief) -> str:
    """演示模式：首节/全文开篇的态势快照素材提示。"""
    rows = []
    if event_brief.title:
        rows.append(f"| 事件名称 | {event_brief.title} |")
    if event_brief.time_range:
        rows.append(f"| 时间 | {event_brief.time_range} |")
    if event_brief.locations:
        rows.append(f"| 地点 | {'、'.join(event_brief.locations[:5])} |")
    if event_brief.entities:
        rows.append(f"| 主要力量/单位 | {'、'.join(event_brief.entities[:6])} |")
    if event_brief.event_types:
        rows.append(f"| 事件类型 | {'、'.join(event_brief.event_types[:3])} |")
    if not rows:
        return (
            "【演示开篇】请根据附件概述，先输出 **态势快照** 表格（项目|内容），"
            "再写 3～4 句总览。"
        )
    table = "\n".join(["| 项目 | 内容 |", "| --- | --- |"] + rows)
    return (
        "【演示开篇·态势快照素材】请据此生成开篇表格（可微调措辞，勿改事实）：\n"
        f"{table}\n"
        "表格后接 3～4 句 Executive Summary。"
    )


def build_polish_system() -> str:
    return POLISH_DEMO_SYSTEM if is_demo_mode() else POLISH_SYSTEM


def build_polish_user_prompt(
    *,
    draft_markdown: str,
    template_name: str = "",
    event_brief: Optional[EventBrief] = None,
) -> str:
    draft = (draft_markdown or "").strip()
    max_chars = 28000
    if len(draft) > max_chars:
        head = draft[: max_chars // 2]
        tail = draft[-(max_chars // 2) :]
        draft = head + "\n\n…（中间省略）…\n\n" + tail
    name = template_name.strip() or "报告模板"
    parts = [
        f"【输出模板】{name}",
        "请润色下列 Markdown 报告草稿，输出完整成稿：",
    ]
    if is_demo_mode() and event_brief is not None:
        parts.append(build_demo_opening_hint(event_brief))
        parts.append("演示润色：弱化库内缺口表述，强化表格与处置清单，保证各节饱满。")
    parts.extend(["", draft])
    return "\n".join(parts)

PLANNER_SYSTEM = """你是内场辅助决策报告规划助手。
- 【待分析事件】来自用户附件，是报告叙述的主线。
- 【达梦 TROOPS 库】仅用于补充可验证事实，不得覆盖附件已明确的时间线。
- 根据【报告模板生成要求】拆出 2～4 个逻辑章节，并为每节列出需要向库内/知识库查询的信息需求（不要写正文）。
- 输出唯一 JSON 对象（不要 markdown 代码块）：
{
  "sections": [
    {
      "section_id": "s1_overview",
      "title": "章节标题（与模板结构对齐）",
      "guidance": "本节写作要点（一句话）",
      "needs": [
        {
          "id": "need_1",
          "description": "需要辅证的事实描述",
          "keywords": ["检索词1", "检索词2"],
          "suggested_tables_hint": ["战斗日志", "部署表"],
          "source": "troops",
          "priority": "required"
        }
      ]
    }
  ]
}
source 取值：troops（达梦库）| knowledge（知识库条令/背景）| both。
每节 1～2 个 needs；keywords 要具体。"""


def build_report_writer_system(*, section_mode: bool = False) -> str:
    """Report Agent 成稿 system prompt（尊重 EVENT2_DEMO_MODE）。"""
    demo = is_demo_mode()
    base = WRITER_DEMO_SYSTEM if demo else WRITER_SYSTEM
    style = WRITER_DEMO_STYLE_RULES if demo else WRITER_STYLE_RULES
    mode_line = (
        "当前为【演示/演习模式】（EVENT2_DEMO_MODE=true）："
        "库内无直接命中时仍须基于附件概述产出完整、饱满、可演示的报告；"
        "正文勿反复出现「库内暂无/依据不足」；禁止伪造溯源角标。"
        if demo
        else "当前为【严肃模式】：无依据须诚实说明材料不足。"
    )
    parts = [
        base,
        style,
        mode_line,
        current_time_block(),
        "【硬性纪律】",
        active_fact_discipline(),
    ]
    if section_mode:
        parts.append(SECTION_WRITER_DEMO_ADDON if demo else SECTION_WRITER_ADDON)
    return "\n\n".join(parts)


def _format_evidence_lines(
    *,
    section_evidences: list[SectionEvidence],
    sections: list[SectionPlan],
    only_section_id: str = "",
) -> list[str]:
    demo = is_demo_mode()
    lines: list[str] = ["【节级库内辅证】"]
    if demo:
        lines.append(
            "（演示模式：下列库内记录仅作参考，勿在正文中强调「无记录/未命中」。"
            "与附件不符的记录忽略；撰写时以【待分析事件概述】为准。）"
        )
    section_by_id = {s.section_id: s for s in sections}
    for ev in section_evidences:
        if only_section_id and ev.section_id != only_section_id:
            continue
        sec = section_by_id.get(ev.section_id)
        title = ev.section_title or (sec.title if sec else ev.section_id)
        status = ev.status or "unknown"
        if demo:
            lines.append(f"\n### {title}")
        else:
            lines.append(f"\n### {title}（状态：{status}）")
        if ev.hits:
            for idx, hit in enumerate(ev.hits[:8], 1):
                text = (hit.title_or_content or "").replace("\n", " ")[:220]
                lines.append(f"{idx}. [{hit.platform}] {text or '（无摘要）'}")
        else:
            if demo:
                lines.append("（无库内直接命中；请依据附件事件概述撰写，正文勿写「库内暂无」）")
            else:
                lines.append("（库内暂无与本节相关的辅证记录）")
        if ev.kb_summary:
            lines.append(f"知识库辅证：\n{ev.kb_summary[:800]}")
        if ev.gaps and not demo:
            lines.append("待补信息：" + "；".join(ev.gaps[:4]))
        elif ev.gaps and demo:
            lines.append("（内部提示，勿写入正文）：" + "；".join(ev.gaps[:2]))
    return lines


def build_planner_user_prompt(
    *,
    event_brief: EventBrief,
    template: ReportTemplate,
    topic: str,
) -> str:
    return "\n\n".join([
        f"【事件标签】\n{topic}",
        f"【待分析事件概述】\n{format_event_brief_block(event_brief)}",
        f"【报告模板：{template.name}（{template.id}）】",
        "【模板说明（节选）】",
        (template.guidance_content or "")[:1500],
        "【生成要求（节选）】",
        (template.generation_content or "")[:2000],
        "请输出 sections JSON。",
    ])


def build_writer_user_prompt(
    *,
    topic: str,
    event_brief: EventBrief,
    template: ReportTemplate,
    metadata: dict,
    section_evidences: list[SectionEvidence],
    sections: list[SectionPlan],
    knowledge_summary: str = "",
    citation_catalog: str = "",
) -> str:
    from singleAgent.context import build_fast_context

    evidence_lines = _format_evidence_lines(
        section_evidences=section_evidences,
        sections=sections,
    )
    kb = knowledge_summary
    if not kb:
        kb_parts = [e.kb_summary for e in section_evidences if e.kb_summary]
        kb = "\n\n".join(kb_parts)

    ctx = build_fast_context(
        topic=topic,
        query=topic,
        metadata=metadata,
        search_summary="\n".join(evidence_lines),
        knowledge_summary=kb,
        event_brief=event_brief,
        primary_event_doc=True,
    )

    if is_demo_mode():
        closing = (
            "\n请按模板生成完整 Markdown 报告。"
            "先输出 **态势快照** 表格与 Executive Summary，再写各章。"
            "即使库内辅证为空，也必须基于【待分析事件概述】写满各章节；"
            "正文勿反复写「依据不足/库内暂无」；末行可加材料说明引用块。"
        )
    else:
        closing = "\n请按模板生成完整 Markdown 报告。partial/empty 节须诚实标注缺口。"

    parts = [
        template.to_prompt_block(topic=topic),
        "",
        ctx.user_prompt,
    ]
    if is_demo_mode():
        parts.insert(2, build_demo_opening_hint(event_brief))
    if citation_catalog:
        parts.extend(["", citation_catalog])
    parts.append(closing)
    return "\n".join(parts)


def build_section_writer_user_prompt(
    *,
    topic: str,
    event_brief: EventBrief,
    template: ReportTemplate,
    metadata: dict,
    section: SectionPlan,
    evidence: SectionEvidence,
    all_sections: list[SectionPlan],
    citation_catalog: str = "",
    knowledge_summary: str = "",
) -> str:
    """P3：单节成稿 prompt。"""
    from singleAgent.context import build_fast_context

    evidence_lines = _format_evidence_lines(
        section_evidences=[evidence],
        sections=all_sections,
        only_section_id=section.section_id,
    )
    kb = knowledge_summary or evidence.kb_summary or ""

    ctx = build_fast_context(
        topic=topic,
        query=topic,
        metadata=metadata,
        search_summary="\n".join(evidence_lines),
        knowledge_summary=kb,
        event_brief=event_brief,
        primary_event_doc=True,
    )

    heading_hint = section.title
    if not heading_hint.startswith("#"):
        heading_hint = f"## {section.title}"

    closing = (
        f"\n请只输出本节「{section.title}」的 Markdown。\n"
        f"建议标题行：{heading_hint}\n"
        f"本节写作要点：{section.guidance or '按模板要求'}\n"
    )
    if is_demo_mode():
        closing += "遵守【演示版式】：本节结论先行，至少 3 条要点或 1 张表格。\n"
        if _is_first_section(section, all_sections):
            closing += build_demo_opening_hint(event_brief) + "\n"
        closing += "演示模式：库内无辅证时直接依据附件写满，正文勿写「库内暂无/依据不足」。\n"
    else:
        closing += "遵守【版式与文体】：结论先行，要点列表/表格优先。\n"
        if evidence.status in {"partial", "empty", "skipped"}:
            closing += "本节库内辅证不足，正文须诚实写明缺口。\n"
    if _is_advice_section(section.title):
        closing += (
            "本节须输出编号处置清单，格式："
            "N. **对象**：动作｜优先级：高/中/低｜时限：…\n"
        )

    parts = [
        f"【输出模板：{template.name}（{template.id}）】",
        template.generation_content[:1200] if template.generation_content else "",
        "",
        f"【当前章节】{section.title}（{section.section_id}）",
        ctx.user_prompt,
    ]
    if citation_catalog:
        parts.extend(["", "【本节可用溯源】", citation_catalog])
    parts.append(closing)
    return "\n".join(p for p in parts if p is not None)
