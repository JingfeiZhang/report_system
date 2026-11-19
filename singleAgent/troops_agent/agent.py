"""TROOPS MCP Agent — P0：注释选表 → describe → 单条 SQL → 执行/采样。"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

from singleAgent.dm_mcp_search import McpSearchHit, mcp_payload_to_hits
from singleAgent.llm import complete_chat
from singleAgent.troops_agent.models import AgentContextBundle, AgentTraceStep
from singleAgent.troops_agent.prompts import (
    PLAN_SQL_SYSTEM,
    SELECT_TABLES_SYSTEM,
    build_plan_sql_user_prompt,
    build_select_tables_user_prompt,
    build_sql_fix_user_prompt,
)
from singleAgent.troops_agent.tools import ToolExecutor, open_dm_connection


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = _strip_fence(raw)
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


def _agent_int(name: str, default: int) -> int:
    return max(1, int(os.getenv(name, str(default))))


def _agent_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


_FALLBACK_COMMENT_KEYWORDS = (
    "战斗",
    "部署",
    "部队",
    "机动",
    "日志",
    "事件",
    "作战",
    "兵力",
    "军队",
)


def fallback_select_tables(
    topic: str,
    catalog: Sequence[Dict[str, Any]],
    *,
    max_pick: int,
) -> List[str]:
    from singleAgent.troops_agent.tools import filter_business_catalog

    catalog = filter_business_catalog(catalog)
    allow = os.getenv("DM_TROOPS_TABLE_ALLOWLIST") or os.getenv("DM_MCP_TABLE_ALLOWLIST")
    if allow:
        allow_set = {t.strip().upper() for t in allow.split(",") if t.strip()}
        out = [str(i["table"]) for i in catalog if str(i.get("table", "")).upper() in allow_set]
        return out[:max_pick]

    topic_l = (topic or "").lower()
    scored: List[tuple[int, str]] = []
    for item in catalog:
        name = str(item.get("table") or "")
        comment = str(item.get("comment") or "")
        blob = f"{name} {comment}"
        score = 0
        for kw in _FALLBACK_COMMENT_KEYWORDS:
            if kw in topic or kw in comment or kw in blob:
                score += 2
        for ch in topic_l:
            if ch.strip() and ch in comment:
                score += 1
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if scored:
        return [n for _, n in scored[:max_pick]]
    return [str(catalog[i]["table"]) for i in range(min(max_pick, len(catalog)))]


def _llm_select_tables(
    topic: str,
    catalog: Sequence[Dict[str, Any]],
    llm_cfg: Dict[str, str],
) -> tuple[List[str], str]:
    raw = complete_chat(
        api_key=llm_cfg["api_key"],
        base_url=llm_cfg["base_url"],
        model_name=llm_cfg["model_name"],
        system_prompt=SELECT_TABLES_SYSTEM,
        user_prompt=build_select_tables_user_prompt(topic, catalog),
        max_tokens=_agent_int("TROOPS_AGENT_SELECT_MAX_TOKENS", 512),
        temperature=float(os.getenv("TROOPS_AGENT_LLM_TEMPERATURE", "0")),
        timeout=_agent_float("TROOPS_AGENT_LLM_TIMEOUT_SEC", 45),
        check_connectivity=False,
    )
    data = _parse_json_object(raw)
    if not data:
        return [], "llm_json_parse_failed"
    tables_raw = data.get("tables") or []
    valid_names = {
        str(i.get("table") or "").upper(): str(i.get("table") or "")
        for i in catalog
        if i.get("table")
    }
    picked: List[str] = []
    for t in tables_raw:
        key = str(t).upper()
        if key in valid_names:
            name = valid_names[key]
            if name not in picked:
                picked.append(name)
    reason = str(data.get("reason") or "")
    return picked, reason or "llm"


def _llm_plan_sql(
    topic: str,
    table: str,
    describe: Dict[str, Any],
    llm_cfg: Dict[str, str],
    *,
    fix_prompt: Optional[str] = None,
) -> Optional[str]:
    user = fix_prompt or build_plan_sql_user_prompt(topic, table, describe)
    raw = complete_chat(
        api_key=llm_cfg["api_key"],
        base_url=llm_cfg["base_url"],
        model_name=llm_cfg["model_name"],
        system_prompt=PLAN_SQL_SYSTEM,
        user_prompt=user,
        max_tokens=_agent_int("TROOPS_AGENT_SQL_MAX_TOKENS", 768),
        temperature=float(os.getenv("TROOPS_AGENT_LLM_TEMPERATURE", "0")),
        timeout=_agent_float("TROOPS_AGENT_LLM_TIMEOUT_SEC", 45),
        check_connectivity=False,
    )
    data = _parse_json_object(raw)
    if not data:
        return None
    queries = data.get("queries") or []
    if not queries:
        return None
    first = queries[0]
    if isinstance(first, dict):
        return str(first.get("sql") or "").strip() or None
    return None


def _rows_to_hits(
    schema: str,
    table: str,
    rows: Sequence[Dict[str, Any]],
    *,
    topic: str,
) -> List[McpSearchHit]:
    if not rows:
        return []
    hits_payload = []
    for row in rows:
        snippet_parts = [f"{k}={v}" for k, v in list(row.items())[:8]]
        hits_payload.append(
            {
                "table": table,
                "row": row,
                "snippet": " | ".join(snippet_parts)[:400],
            }
        )
    payload = {"schema": schema, "hits": hits_payload}
    label = (topic or "Agent")[:24]
    return mcp_payload_to_hits(payload, keyword=f"[Agent·{label}]")


def _build_summary(bundle: AgentContextBundle) -> str:
    lines = [
        f"数据源：达梦 schema「{bundle.schema}」（TROOPS Agent 查库，非 mock）",
    ]
    if bundle.topic.strip():
        lines.append(f"讨论主题：{bundle.topic.strip()}")
    if bundle.selected_tables:
        lines.append(f"选表：{'、'.join(bundle.selected_tables)}")
    lines.append(f"共 {len(bundle.hits)} 条记录片段：")
    for idx, row in enumerate(bundle.hits[:15], 1):
        text = (row.title_or_content or "").replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"{idx}. [{row.platform}] {text or '（无摘要）'}")
    if len(bundle.hits) > 15:
        lines.append(f"… 另有 {len(bundle.hits) - 15} 条未列出")
    if bundle.errors:
        lines.append("备注：" + "；".join(bundle.errors[:4]))
    return "\n".join(lines)


class TroopsMcpAgent:
    def run(
        self,
        *,
        topic: str,
        instruction: str = "",
        llm_cfg: Optional[Dict[str, str]] = None,
    ) -> AgentContextBundle:
        subject = (topic or instruction or "").strip()
        max_pick = _agent_int("TROOPS_AGENT_SELECT_MAX_TABLES", 3)
        max_sql_retries = _agent_int("TROOPS_AGENT_SQL_RETRIES", 2)
        sample_limit = _agent_int("TROOPS_AGENT_SAMPLE_LIMIT", 3)
        budget_sec = _agent_float("TROOPS_AGENT_BUDGET_SEC", 90)
        started = time.time()

        trace: List[AgentTraceStep] = []
        errors: List[str] = []
        hits: List[McpSearchHit] = []
        selected: List[str] = []
        schema = ""

        def budget_left() -> float:
            return budget_sec - (time.time() - started)

        if not llm_cfg:
            errors.append("未提供 LLM 配置，Agent 无法选表与写 SQL")
            bundle = AgentContextBundle(
                topic=subject,
                errors=errors,
                empty=True,
                summary_text="TROOPS Agent 需要 LLM 配置（model_config）。",
                trace=trace,
            )
            return bundle

        conn = None
        try:
            conn = open_dm_connection()
            tools = ToolExecutor(conn, os.getenv("DM_MCP_SCHEMA", "TROOPS_2_ENHANCED"))
            schema = tools.schema
            trace.append(AgentTraceStep("INIT", f"已连接达梦 schema={schema}"))

            catalog = tools.list_catalog()
            trace.append(
                AgentTraceStep("CATALOG", f"业务表 {len(catalog)} 张（含注释）")
            )
            if not catalog:
                errors.append("无可用业务表")
                bundle = AgentContextBundle(
                    schema=schema,
                    topic=subject,
                    errors=errors,
                    empty=True,
                    trace=trace,
                    summary_text=f"schema {schema} 无可用业务表",
                )
                return bundle

            picked, pick_src = _llm_select_tables(subject, catalog, llm_cfg)
            if not picked:
                picked = fallback_select_tables(subject, catalog, max_pick=max_pick)
                pick_src = "fallback"
            selected = picked[:max_pick]
            trace.append(
                AgentTraceStep(
                    "SELECT_TABLES",
                    f"选中 {len(selected)} 张表（{pick_src}）：{'、'.join(selected)}",
                )
            )

            primary = selected[0]
            desc = tools.describe(primary)
            trace.append(
                AgentTraceStep(
                    "DESCRIBE",
                    f"{primary} 列数 {len(desc.get('columns') or [])}",
                )
            )

            sql = _llm_plan_sql(subject, primary, desc, llm_cfg)
            if not sql:
                sql = f"SELECT * FROM {primary} WHERE ROWNUM <= {sample_limit}"
                errors.append("LLM 未产出 SQL，使用默认采样")
                trace.append(AgentTraceStep("PLAN_SQL", "使用默认 ROWNUM 采样 SQL"))

            executed = False
            for attempt in range(max_sql_retries + 1):
                if budget_left() <= 0:
                    errors.append("Agent 时间预算耗尽")
                    break
                result = tools.execute(sql)
                if result.get("ok") and result.get("rows"):
                    hits.extend(
                        _rows_to_hits(schema, primary, result["rows"], topic=subject)
                    )
                    trace.append(
                        AgentTraceStep(
                            "EXECUTE",
                            f"SQL 命中 {result['row_count']} 行",
                            {"sql": result.get("sql_executed", sql)},
                        )
                    )
                    executed = True
                    break
                err = result.get("error") or "0 rows"
                if result.get("ok") and not result.get("rows"):
                    trace.append(AgentTraceStep("EXECUTE", f"{primary} 查询 0 行"))
                    break
                trace.append(AgentTraceStep("EXECUTE", f"SQL 失败: {err}"))
                if attempt >= max_sql_retries:
                    errors.append(f"SQL 失败: {err}")
                    break
                fix_user = build_sql_fix_user_prompt(
                    subject, primary, sql, str(err)
                )
                fixed = _llm_plan_sql(
                    subject, primary, desc, llm_cfg, fix_prompt=fix_user
                )
                if fixed:
                    sql = fixed
                else:
                    break

            if not hits:
                sample = tools.sample_rows(primary, limit=sample_limit)
                if sample.get("rows"):
                    hits.extend(
                        _rows_to_hits(schema, primary, sample["rows"], topic=subject)
                    )
                    trace.append(
                        AgentTraceStep(
                            "SAMPLE_FALLBACK",
                            f"{primary} 采样 {sample['row_count']} 行",
                        )
                    )
                else:
                    errors.append(f"表 {primary} 查询与采样均无数据")

            bundle = AgentContextBundle(
                schema=schema,
                topic=subject,
                selected_tables=selected,
                hits=hits,
                trace=trace,
                errors=errors,
                empty=not hits,
            )
            bundle.summary_text = _build_summary(bundle)
            logger.info(
                "TroopsMcpAgent schema={} tables={} hits={} pick={}",
                schema,
                selected,
                len(hits),
                pick_src,
            )
            return bundle
        except Exception as exc:
            logger.exception("TroopsMcpAgent failed")
            errors.append(str(exc))
            bundle = AgentContextBundle(
                schema=schema,
                topic=subject,
                selected_tables=selected,
                hits=hits,
                trace=trace,
                errors=errors,
                empty=not hits,
                summary_text=f"TROOPS Agent 异常：{exc}",
            )
            return bundle
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
