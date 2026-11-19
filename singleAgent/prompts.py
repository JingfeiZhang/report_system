"""singleAgent 普适提示词（事件无关）；输出格式由 report_templates/ 注入。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from singleAgent.config import EVENT2_DEMO_MODE
from singleAgent.report_template_loader import load_report_template, resolve_template_id

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

# 业务约定：提示词中的“现在”统一按此时区表述
PROMPT_TIMEZONE = "Asia/Shanghai"

FACT_DISCIPLINE = """执行优先级：事实依据 > 用户要求 > 模板结构 > 表达风格。
1. 只使用【用户输入】【近期对话】【库内检索摘要】【知识库检索摘要】【所选案例】和【附件材料】中的事实。
2. 没有依据时明确写“现有材料未提供”，不得补造数字、时间、机构观点、传播规模或典型案例。
3. 多类材料冲突时分别陈述并说明差异，不自行判断哪一方必然正确。
4. 严格使用所选模板的结构；每段先写结论，再写依据，避免重复和空泛套话。
5. 只输出最终 Markdown 正文，不输出思考过程、模板说明、生成要求或“以下是报告”等元话语。
6. 不输出免责声明、编制说明和重复总结；达到目标篇幅后立即结束，不要求二次修复生成。
7. 若用户消息含【溯源标注】与可用编号，在有依据的事实句末标注半角形式 (标N)；勿编造未列出的编号，勿另附参考文献列表。
8. 以【当前时间】为“现在”；“近期/当前/截至目前”等时态必须相对该时间表述，不得按训练知识臆测今天是哪一天。
9. 引用检索材料时优先带上材料自带日期；材料为“日期待考”或无日期时，不得写成“最新进展/今天发生”。
10. 禁止把明显早于【当前时间】的材料表述成当下刚发生的事实。"""

DEMO_FACT_DISCIPLINE = """执行优先级：演示完整度 > 模板结构 > 表达风格；有检索命中时仍优先用命中事实。
1. 本轮为【演示模式】：目标是产出结构完整、读起来像样的分析，供甲方演示；不以“库内无命中就空答”为优先。
2. 优先使用【待分析事件概述】【用户输入】【近期对话】【库内检索摘要】【知识库检索摘要】【所选案例】【附件材料】中的内容。
3. 若库内/知识库无命中或明显不足：允许结合【待分析事件概述】/附件/案例与公开常识、合理推演补全各章节，使报告版式与研判链条完整。
4. 有检索命中时：冲突处分别陈述；无命中时：用“基于案例材料与公开信息的演示研判”口吻表述，勿假装“库内已证实”。
5. 严格使用所选模板的结构；每段先写结论，再写依据，避免空泛套话与元话语。
6. 只输出最终 Markdown 正文；不输出思考过程、模板说明、“以下是报告”等。
7. 不输出免责声明长文、编制说明和重复总结；达到目标篇幅后立即结束。
8. 若用户消息含【溯源标注】与可用编号：仅对真实列出的编号标注 (标N)；禁止编造未列出的标号，禁止伪造参考文献或虚假溯源。
9. 以【当前时间】为“现在”；“近期/当前/截至目前”必须相对该时间表述。
10. 引用有日期的材料时带上日期；无日期材料勿写成“今天刚发生”。"""

CHAT_DISCIPLINE = """【纪律】
1. 优先依据【达梦检索摘要】与【知识库摘要】作答；没有证据时明确说依据不足，勿编造精确统计。
2. 回答宜适中篇幅，用 Markdown；像研讨助手，而不是套用固定报告模板。
3. 若用户只要短答/追问，直接回答，不要硬扩成全文报告。
4. 语气专业、克制。
5. 若用户消息含【溯源标注】与可用编号，可在有依据的句子末标注 (标N)；勿编造未列出的编号。
6. 以【当前时间】为“现在”；“近期/当前/截至目前”必须相对该时间表述，不得按训练知识臆测今天是哪一天。
7. 引用材料时优先带上材料自带日期；材料为“日期待考”或无日期时，不得写成“最新进展/今天发生”。
8. 禁止把明显早于【当前时间】的材料表述成当下刚发生的事实。"""

DEMO_CHAT_DISCIPLINE = """【纪律｜演示模式】
1. 本轮为演示：优先把讨论写完整、像样；有【达梦检索摘要】/【知识库摘要】命中时仍优先引用命中。
2. 库内无明显命中时：允许结合用户消息、附件/案例与公开常识作合理推演，勿只回复“依据不足/现有材料未提供”。
3. 回答宜适中篇幅，用 Markdown；像研讨助手，不要硬套固定长报告模板（除非用户明确要长文）。
4. 语气专业、克制；无库内证据时用“演示研判/基于案例与公开信息”口吻，勿假装库内已证实。
5. 若用户消息含【溯源标注】与可用编号：仅标注真实列出的 (标N)；禁止编造未列出的编号。
6. 以【当前时间】为“现在”；时态相对该时间表述。
7. 有日期的材料带上日期；无日期勿写成“今天刚发生”。"""


def is_demo_mode() -> bool:
    return bool(EVENT2_DEMO_MODE)


def active_fact_discipline() -> str:
    return DEMO_FACT_DISCIPLINE if is_demo_mode() else FACT_DISCIPLINE


def active_chat_discipline() -> str:
    return DEMO_CHAT_DISCIPLINE if is_demo_mode() else CHAT_DISCIPLINE


def format_now_for_prompt() -> str:
    """返回提示词用的当前时间字符串（精确到分钟）。"""
    if ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(PROMPT_TIMEZONE))
            return now.strftime(f"%Y-%m-%d %H:%M（{PROMPT_TIMEZONE}）")
        except Exception:
            pass
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M（本地时区）")


def current_time_block() -> str:
    return f"【当前时间】{format_now_for_prompt()}"


def build_system_prompt(
    *,
    topic: str,
    metadata: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
    template_id: Optional[str] = None,
) -> str:
    meta = metadata or {}
    tid = template_id or resolve_template_id(meta)
    session_id = str(meta.get("session_id") or "").strip() or None
    template = load_report_template(
        tid, project_root=project_root, session_id=session_id
    )
    mode_line = (
        "当前为【演示模式】（EVENT2_DEMO_MODE=true）：无库命中也可生成完整像样分析；禁止伪造溯源角标。"
        if is_demo_mode()
        else "当前为【严肃模式】：无依据须诚实说明材料不足，不得补造事实。"
    )
    return "\n".join([
        "你是一名辅助决策舆情分析助手（检索增强）。",
        "请基于用户问题、【库内检索摘要】【知识库检索摘要】、案例与附件作答。",
        mode_line,
        "",
        current_time_block(),
        "",
        "【硬性纪律】",
        active_fact_discipline(),
        "",
        template.to_prompt_block(topic=topic),
    ])


# 兼容旧引用；导入时快照，正式路径请调用 build_system_prompt(...)
SYSTEM_PROMPT = build_system_prompt(topic="{topic}")


REVISE_MODE_ADDON = """
【修订模式 — 在已有报告基础上整篇重写】
1. 必须输出**完整修订稿**（非片段）；可换版式/结构，但事实仍须遵守上文纪律。
2. 若用户要求「按所选模板重新生成」，只调整叙述结构与章节，**主题不变**，勿重新编造库内未提供的精确统计。
3. 不要单独输出「修订说明」或元话语。"""


def build_revise_system_prompt() -> str:
    return "\n".join([
        "你是一名辅助决策舆情分析助手。用户已在会话中有一份分析回答，现在需要基于原回答、近期对话与修订要求，输出一版**完整**的修订后 Markdown 内容。",
        "",
        "【硬性纪律】",
        active_fact_discipline(),
        REVISE_MODE_ADDON.strip(),
        "",
        current_time_block(),
    ])


# 兼容旧引用（导入时快照）；正式路径请用 build_revise_system_prompt()
REVISE_SYSTEM_PROMPT = (
    "你是一名辅助决策舆情分析助手。用户已在会话中有一份分析回答，现在需要基于原回答、近期对话与修订要求，输出一版**完整**的修订后 Markdown 内容。\n\n"
    "【硬性纪律】\n"
    + FACT_DISCIPLINE
    + REVISE_MODE_ADDON
)


def build_user_prompt(user_body: str) -> str:
    body = (user_body or "").strip()
    if not body:
        return current_time_block()
    if body.startswith("【当前时间】"):
        return body
    return f"{current_time_block()}\n\n{body}"


ROUTE_SYSTEM_PROMPT = """你是辅助决策系统的「意图分类器」。请先判断用户是在**延续当前主题**，还是**切换到新主题/需要重新查库**，再选择 mode。

【核心逻辑 — 最高优先级】
- **mode 0（新分析）≈ 换主题或必须重新检索**：用户提出了**新事件/新主题**，或明确要求**重新检索/换主题/对新案例·附件做分析**。需要查舆情库后再生成完整报告。
- **mode 2（修改定稿）≈ 同主题延续**：会话**已有报告**，且用户仍在讨论**同一分析主题**，只是要**整篇重写/换版式/合并修订后重新生成**。典型话术：「重新生成」「按所选模板生成」「出一版完整新版」「定稿」——**即使出现「报告」「生成」字样，只要主题未变，仍是 mode 2，不是 0**。
- **mode 1（对话）**：同主题下的**轻量交流**，只解释、闲聊、或**局部**改一段，**不要**完整长文。

【判断步骤】
1. 看「会话锚定主题 anchor_topic」与「是否已有报告」。
2. 若已有报告：用户本条是否引入**全新事件名/新话题**？否 → 优先 mode 2 或 1；是 → mode 0。
3. 若尚无报告：用户给出事件/主题 → mode 0；纯问候/短问 → mode 1。
4. 「重新**生成**报告」与「重新**检索**/换主题」不同：前者多为 mode 2，后者为 mode 0。

【示例】
- 已有报告，anchor=美伊以战争，用户说「请根据所选模板重新生成一份报告」→ mode 2（同主题换版式，不查库）
- 已有报告，用户说「换一个主题分析俄乌冲突」→ mode 0（新主题，需检索）
- 无报告，用户说「美伊以战争」→ mode 0
- 已有报告，用户说「第一段再具体一点」→ mode 1（局部修订）

【输出要求】
只输出一行 JSON，不要 markdown 代码块，不要其它文字：
{"mode":0,"reason":"不超过30字的理由"}
mode 只能是整数 0、1 或 2。"""


CHAT_SYSTEM_PROMPT = (
    "你是一名辅助决策舆情分析对话助手。当前为自由讨论模式：多角度聊清事件，可引用检索证据，但不要写成正式版式长报告。\n\n"
    + CHAT_DISCIPLINE
)


def build_chat_system_prompt() -> str:
    """讨论态 system：按 EVENT2_DEMO_MODE 选纪律 + 本轮当前时间。"""
    header = (
        "你是一名辅助决策舆情分析对话助手。当前为自由讨论模式：多角度聊清事件，可引用检索证据，但不要写成正式版式长报告。"
    )
    mode_line = (
        "当前为【演示模式】：无库命中也可给出像样讨论；禁止伪造溯源角标。"
        if is_demo_mode()
        else "当前为【严肃模式】：无依据须说明材料不足。"
    )
    return "\n".join([
        header,
        mode_line,
        "",
        active_chat_discipline(),
        "",
        current_time_block(),
    ])


TOPIC_EXTRACT_SYSTEM_PROMPT = """你是舆情分析系统的「主题抽取器」。结合会话上下文和用户本条指令，提取**唯一分析主题**（事件名/议题名）。

【要求】
1. 主题是简短名词短语，2～20 字，不要整句，不要「请生成…报告」这类操作话术。
2. 若用户指令嵌套在祈使句中（如「请生成美伊以战争的报告」），主题应为「美伊以战争」。
3. 若本条只是「按模板生成」「突出风险和建议」等格式或侧重点要求，必须从最近会话中判断报告涉及的事件。
4. 若上下文包含多个事件，优先选择用户最近明确要求生成报告的事件；只有明确换题时才使用新主题。
5. 不要把模板名称、报告类型、风险、建议等写作要求当成事件主题。
6. 不要输出检索词列表，只要主题。

【输出】
只输出一行 JSON：
{"topic":"主题短语","reason":"不超过20字"}"""


SEARCH_TERMS_SYSTEM_PROMPT = """你是舆情分析系统的「检索词规划器」。根据**已确定的分析主题**，生成适合数据库 LIKE 检索的短关键词。

【要求】
1. 词必须锚定主题实体（地名、国名、事件常用称呼、关键人物/机构简称），2～8 个汉字为宜。
2. 禁止输出操作/模板元话术：请、生成、报告、模板、重新、所选、根据、一份、完整、帮我、分析一下等。
3. 输出 2～4 个词，互不重复，优先网民库内可能出现的叫法。
4. 不要输出长句或问句。

【输出】
只输出一行 JSON：
{"terms":["词1","词2"],"reason":"不超过20字"}"""


SEARCH_REFINE_SYSTEM_PROMPT = """你是舆情分析系统的「补搜词规划器」。已有一轮检索结果，请在**同一主题**下补充新的检索词。

【要求】
1. 必须仍围绕给定主题，不得换成无关热点。
2. 新词不得与「已用检索词」重复。
3. 可依据上轮命中标题中的空白点补充别称、相关地点、关键子议题（仍须与主题相关）。
4. 若上轮已经够用或无明显可补，输出空数组 terms=[]。
5. 禁止操作元话术；词长 2～8 字；最多 3 个新词。

【输出】
只输出一行 JSON：
{"terms":["词1"],"reason":"不超过20字"}"""


def build_topic_extract_user_prompt(
    *,
    instruction: str,
    session_topic: str = "",
    conversation_context: Optional[List[Dict[str, str]]] = None,
) -> str:
    context_lines: List[str] = []
    for item in (conversation_context or [])[-12:]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        role = str(item.get("role") or "").strip().lower()
        label = "用户" if role in ("human", "user") else "助手"
        context_lines.append(f"{label}：{content[:1200]}")
    context_text = "\n".join(context_lines) or "（无）"
    return "\n".join([
        f"【会话旧主题】\n{session_topic.strip() or '（无）'}",
        f"【最近会话上下文】\n{context_text}",
        f"【用户本条指令】\n{instruction.strip() or '（空）'}",
        "",
        "请提取本轮分析主题。",
    ])


def build_search_terms_user_prompt(
    *,
    topic: str,
    instruction: str = "",
) -> str:
    return "\n".join([
        f"【分析主题】\n{topic.strip()}",
        f"【用户原指令（仅供理解，勿把操作词当检索词）】\n{instruction.strip() or '（无）'}",
        "",
        "请生成数据库检索关键词。",
    ])


def build_search_refine_user_prompt(
    *,
    topic: str,
    used_terms: list[str],
    hit_summary: str,
) -> str:
    return "\n".join([
        f"【分析主题】\n{topic.strip()}",
        f"【已用检索词】\n{'、'.join(used_terms) if used_terms else '（无）'}",
        f"【上轮检索摘要】\n{(hit_summary or '暂无命中').strip()[:1200]}",
        "",
        "请给出补充检索词；若无需补充则 terms=[]。",
    ])


def build_route_user_prompt(
    *,
    instruction: str,
    has_report: bool,
    last_topic: str,
    has_ext: bool,
    has_files: bool,
    anchor_topic: str = "",
) -> str:
    anchor = (anchor_topic or last_topic or "").strip()
    return "\n".join([
        "【会话状态】",
        f"- 是否已有分析报告：{'是' if has_report else '否'}",
        f"- 会话锚定主题 anchor_topic：{anchor or '（无，本条可能是首轮）'}",
        f"- 是否附带案例(ext)：{'是' if has_ext else '否'}",
        f"- 是否附带附件：{'是' if has_files else '否'}",
        "",
        "【用户本条消息】",
        instruction.strip() or "（空）",
        "",
        "请判断：用户是在延续 anchor_topic，还是提出了需要重新检索的新主题？",
    ])


def build_chat_user_prompt(
    *,
    instruction: str,
    topic: str,
    last_report_md: str,
    recent_messages: list[dict[str, str]],
    search_summary: str = "",
    knowledge_summary: str = "",
    citation_catalog: str = "",
) -> str:
    parts: list[str] = [
        current_time_block(),
        f"【当前会话主题】\n{topic or '（未指定）'}",
    ]

    if search_summary:
        parts.append(f"【达梦检索摘要】\n{search_summary}")
    else:
        parts.append("【达梦检索摘要】\n（本轮无命中或未检索）")

    if knowledge_summary:
        parts.append(f"【知识库摘要】\n{knowledge_summary}")

    if last_report_md:
        parts.append(f"【上一轮报告】\n{last_report_md}")
    else:
        parts.append("【上一轮报告】\n（暂无）")

    if recent_messages:
        lines = []
        for msg in recent_messages[-6:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            lines.append(f"{role}：{msg.get('content', '')}")
        parts.append("【近期对话】\n" + "\n".join(lines))

    if citation_catalog:
        parts.append(citation_catalog)

    parts.append(f"【用户本条消息】\n{instruction.strip()}")
    return "\n\n".join(parts)


def build_revise_user_prompt(
    *,
    instruction: str,
    topic: str,
    last_report_md: str,
    last_search_summary: str,
    recent_messages: list[dict[str, str]],
) -> str:
    parts: list[str] = [
        current_time_block(),
        f"【分析主题】\n{topic or '（未指定）'}",
        f"【当前报告全文】\n{last_report_md or '（无）'}",
    ]
    if last_search_summary:
        parts.append(f"【首轮检索摘要（仅供参考，勿重新检索）】\n{last_search_summary}")

    if recent_messages:
        lines = []
        for msg in recent_messages[-10:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            lines.append(f"{role}：{msg.get('content', '')}")
        parts.append("【近期对话（含段落细化等内容）】\n" + "\n".join(lines))

    parts.append(f"【本次修订要求】\n{instruction.strip()}")
    return "\n\n".join(parts)
