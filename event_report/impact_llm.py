# -*- coding: utf-8 -*-
"""impact 短时 LLM：填叙事字段，失败返回 None（由上层回退骨架）。"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from loguru import logger


def llm_enabled() -> bool:
    raw = (os.getenv("EVENT_REPORT_LLM_ENABLED") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _timeout_sec() -> float:
    try:
        return max(2.0, float(os.getenv("EVENT_REPORT_LLM_TIMEOUT_SEC") or "8"))
    except ValueError:
        return 8.0


def _resolve_llm_cfg() -> Optional[Dict[str, str]]:
    """优先 REPORT_ENGINE_*，其次 INSIGHT_ENGINE_* / OPENAI_*。"""
    for prefix in ("REPORT_ENGINE", "INSIGHT_ENGINE", "OPENAI"):
        if prefix == "OPENAI":
            key = (os.getenv("OPENAI_API_KEY") or "").strip()
            base = (os.getenv("OPENAI_BASE_URL") or "").strip()
            model = (os.getenv("OPENAI_MODEL_NAME") or os.getenv("OPENAI_MODEL") or "").strip()
        else:
            key = (os.getenv(f"{prefix}_API_KEY") or "").strip()
            base = (os.getenv(f"{prefix}_BASE_URL") or "").strip()
            model = (os.getenv(f"{prefix}_MODEL_NAME") or "").strip()
        if key and base and model:
            return {"api_key": key, "base_url": base.rstrip("/"), "model": model}
    return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    # 去掉 markdown 围栏
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(raw[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


_SYSTEM = """你是舆情影响评估助手。根据给定事件标题、摘要与若干舆情帖子，输出严格 JSON（不要 markdown），字段：
{
  "impact_scope": "波及范围短句",
  "affected_population": "受影响人群文案或暂无量化数据",
  "casualties": "伤亡文案或暂无权威伤亡数据",
  "economic_loss": "经济影响文案或暂无量化数据",
  "resolution_result": "后续处理文案或暂无后续处理信息",
  "avg_goldstein": -10到10的数字,
  "avg_tone": -10到10的数字,
  "media_opinions": {
    "domestic": [{"source":"","view":"","sentiment_score":0,"sentiment_label":"","url":null,"published_at":null}],
    "international": []
  }
}
要求：不要编造精确伤亡数字；不确定时用「暂无…」类措辞；media_opinions.domestic 最多5条、international 最多5条；view 不超过60字。"""


def fill_impact_narrative(
    *,
    title: str,
    summary: str,
    event_date: str,
    posts: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """调用 LLM 填叙事；失败返回 None。"""
    if not llm_enabled():
        return None
    cfg = _resolve_llm_cfg()
    if not cfg:
        logger.info("impact LLM 未配置 API，跳过")
        return None

    samples = []
    for i, p in enumerate(posts[:8], 1):
        samples.append(
            f"{i}. [{p.get('platform') or ''}] {p.get('account_name') or ''} | "
            f"{(p.get('content') or '')[:120]}"
        )
    user = (
        f"事件标题：{title}\n"
        f"事件日期：{event_date}\n"
        f"摘要：{(summary or '')[:800]}\n"
        f"舆情样本：\n" + ("\n".join(samples) if samples else "（无）")
    )

    timeout = _timeout_sec()
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            timeout=timeout,
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        data = _extract_json(text)
        if not data:
            logger.warning("impact LLM 返回非 JSON")
            return None
        return data
    except Exception as e:
        logger.warning("impact LLM 失败: {!r}", e)
        return None
