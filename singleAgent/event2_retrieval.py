"""event2 检索入口：Agent / MCP context / legacy。"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from loguru import logger

from singleAgent.dm_mcp_search import is_mcp_search_enabled
from singleAgent.dm_search import SearchBundle, multi_round_search
from singleAgent.mcp_context import (
    McpContextBundle,
    check_dm_context_available,
    fetch_mcp_context,
)


def _agent_topic_from_brief(
    topic: str,
    search_text: str,
    event_brief: Any,
) -> str:
    if event_brief is None:
        return (topic or search_text or "").strip()
    try:
        from singleAgent.report_agent.event_ingest import build_retrieval_topic

        return build_retrieval_topic(
            event_brief,
            fallback_topic=topic,
            fallback_instruction=search_text,
        )
    except ImportError:
        return (topic or search_text or "").strip()


def event2_data_source() -> str:
    raw = (os.getenv("EVENT2_DATA_SOURCE") or "").strip().lower()
    if raw in {"legacy", "mock", "mindspider", "opinion"}:
        return "legacy"
    if raw == "agent":
        return "agent"
    if raw in {"context", "mcp", "dm", "dameng", "troops", "sample"}:
        return "context"
    if is_mcp_search_enabled():
        return "context"
    return "legacy"


def is_event2_agent_retrieval() -> bool:
    return event2_data_source() == "agent"


def is_event2_mcp_retrieval() -> bool:
    """context 盲采样或 agent（均非 legacy mock 舆情）。"""
    return event2_data_source() in {"agent", "context"}


def is_event2_context_retrieval() -> bool:
    return event2_data_source() == "context"


def retrieve_event2_context(
    *,
    topic: str,
    search_text: str = "",
    target_hits: Optional[int] = None,
    llm_cfg: Optional[Dict[str, str]] = None,
    event_brief: Optional[Any] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> SearchBundle:
    """event2 统一检索出口。"""
    src = event2_data_source()

    agent_topic = _agent_topic_from_brief(topic, search_text, event_brief)

    if cancel_check:
        cancel_check()

    if src == "legacy":
        text = (agent_topic or search_text or topic or "").strip()
        return multi_round_search(text, cancel_check=cancel_check)

    if src == "agent":
        from singleAgent.troops_agent.agent import TroopsMcpAgent

        bundle = TroopsMcpAgent().run(
            topic=agent_topic,
            instruction=search_text,
            llm_cfg=llm_cfg,
        )
        return bundle.to_search_bundle()

    ctx = fetch_mcp_context(agent_topic or search_text or topic)
    return mcp_context_to_search_bundle(ctx)


def mcp_context_to_search_bundle(ctx: McpContextBundle) -> SearchBundle:
    if ctx.empty:
        return SearchBundle(
            terms_tried=["[MCP达梦库]"],
            hits=[],
            empty=True,
            summary_text=ctx.summary_text or "达梦库未返回可用上下文。",
        )
    return SearchBundle(
        terms_tried=["[MCP达梦库]"],
        hits=list(ctx.rows),
        empty=False,
        summary_text=ctx.summary_text,
    )


def check_event2_prerequisites() -> tuple[bool, str, str]:
    """返回 (ok, source, message)。"""
    src = event2_data_source()
    if src == "agent":
        from singleAgent.troops_agent.tools import (
            ToolExecutor,
            filter_business_catalog,
            open_dm_connection,
        )
        from mcp_server_dm.schema_search import (
            list_tables_with_comments,
            load_schema_search_config,
        )

        cfg = load_schema_search_config()
        try:
            conn = open_dm_connection()
            try:
                raw = list_tables_with_comments(conn, cfg.schema)
                biz = filter_business_catalog(raw, config=cfg)
                if not biz:
                    return False, "agent", f"schema {cfg.schema} 无可用业务表"
                return True, "agent", f"ok schema={cfg.schema} biz_tables={len(biz)}"
            finally:
                conn.close()
        except Exception as exc:
            return False, "agent", str(exc)

    if src == "context":
        ok, msg = check_dm_context_available()
        if not ok:
            logger.warning("event2 MCP 前置检查失败: {}", msg)
        return ok, "context", msg

    return True, "legacy", "legacy retrieval (mock/mindspider)"
