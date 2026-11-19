"""event2 意图路由：LLM 主判 0/1/2 + 结构性 guardrail。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, Optional

from loguru import logger

from singleAgent.llm import complete_chat
from singleAgent.prompts import ROUTE_SYSTEM_PROMPT, build_route_user_prompt
from singleAgent.session_store import SessionState


@dataclass
class RouteDecision:
    mode: int  # 0=新分析, 1=对话, 2=修改定稿
    reason: str
    source: str  # llm | fallback | guardrail


_CHAT_GREETING = re.compile(r"^(你好|您好|嗨|hello|hi|谢谢|感谢)[!！。~\s]*$", re.I)


def _parse_llm_route(text: str) -> Optional[RouteDecision]:
    raw = (text or "").strip()
    if not raw:
        return None

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("mode") in (0, 1, 2, "0", "1", "2"):
            mode = int(data["mode"])
            reason = str(data.get("reason") or "LLM 分类").strip()[:60]
            return RouteDecision(mode=mode, reason=reason, source="llm")
    except json.JSONDecodeError:
        pass

    m = re.search(r'"mode"\s*:\s*([012])', raw)
    if m:
        return RouteDecision(mode=int(m.group(1)), reason="LLM 分类", source="llm")

    return None


def _fallback_route(
    session: SessionState,
    has_ext: bool,
    has_files: bool,
) -> RouteDecision:
    if has_ext or has_files:
        return RouteDecision(mode=0, reason="附带案例或附件，需新分析", source="fallback")
    if session.has_report:
        return RouteDecision(mode=2, reason="已有报告，默认同主题改版", source="fallback")
    return RouteDecision(mode=0, reason="无报告，默认新分析", source="fallback")


def _apply_guardrails(
    decision: RouteDecision,
    instruction: str,
    session: SessionState,
    has_ext: bool,
    has_files: bool,
) -> RouteDecision:
    """仅 enforce 结构性不变量，语义判断交给 LLM。"""
    text = (instruction or "").strip()

    if has_ext or has_files:
        if decision.mode != 0:
            return RouteDecision(
                mode=0,
                reason="附带新材料，需重新检索分析",
                source="guardrail",
            )
        return decision

    if not session.has_report:
        if decision.mode == 2:
            if _CHAT_GREETING.match(text):
                return RouteDecision(mode=1, reason="问候", source="guardrail")
            return RouteDecision(mode=0, reason="尚无报告，无法改版定稿", source="guardrail")
        if decision.mode == 1 and text and not _CHAT_GREETING.match(text):
            if len(text) <= 48 and "?" not in text and "？" not in text:
                return RouteDecision(mode=0, reason="首轮主题句", source="guardrail")
        return decision

    if decision.mode == 2:
        return decision

    if decision.mode == 0:
        return decision

    return decision


def classify_intent(
    *,
    instruction: str,
    session: SessionState,
    has_ext: bool,
    has_files: bool,
    llm_cfg: Dict[str, str],
    anchor_topic: str = "",
) -> RouteDecision:
    """LLM 主判 0/1/2，失败时 fallback，最后做结构性 guardrail。"""
    anchor = (anchor_topic or session.last_topic or "").strip()
    user_prompt = build_route_user_prompt(
        instruction=instruction,
        has_report=session.has_report,
        last_topic=session.last_topic,
        has_ext=has_ext,
        has_files=has_files,
        anchor_topic=anchor,
    )

    decision: Optional[RouteDecision] = None
    try:
        raw = complete_chat(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            model_name=llm_cfg["model_name"],
            system_prompt=ROUTE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            check_connectivity=True,
        )
        decision = _parse_llm_route(raw)
        if decision is None:
            logger.warning(f"意图分类解析失败，原始输出: {raw[:200]}")
    except Exception as e:
        logger.warning(f"意图分类 LLM 调用失败，使用 fallback: {e}")

    if decision is None:
        decision = _fallback_route(session, has_ext, has_files)

    final = _apply_guardrails(decision, instruction, session, has_ext, has_files)
    logger.info(
        f"event2 route session={session.session_id} mode={final.mode} "
        f"source={final.source} reason={final.reason} anchor={anchor[:40]!r}"
    )
    return final
