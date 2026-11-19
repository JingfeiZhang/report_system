"""卫鼎智能体模板开放接口：list / detail → ReportTemplate。

GET {AGENT_TEMPLATE_API_BASE_URL}/api/v1/ai/agent-template/open/list
GET {AGENT_TEMPLATE_API_BASE_URL}/api/v1/ai/agent-template/open/detail/{id}
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Union

import httpx
from loguru import logger

from singleAgent.config import (
    AGENT_TEMPLATE_API_BASE_URL,
    AGENT_TEMPLATE_API_KEY,
    AGENT_TEMPLATE_TIMEOUT_SEC,
)
from singleAgent.report_template_loader import (
    ReportTemplate,
    TemplateConfigurationError,
    _MAX_OUTPUT_TOKENS,
    _MIN_OUTPUT_TOKENS,
)

_LIST_PATH = "/api/v1/ai/agent-template/open/list"
_DETAIL_PATH = "/api/v1/ai/agent-template/open/detail/{id}"


def remote_templates_configured() -> bool:
    return bool((AGENT_TEMPLATE_API_BASE_URL or "").strip())


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    key = (AGENT_TEMPLATE_API_KEY or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _api_base() -> str:
    return (AGENT_TEMPLATE_API_BASE_URL or "").rstrip("/")


def _unwrap_data(body: Any, *, expect_list: bool = False) -> Any:
    if not isinstance(body, dict):
        raise TemplateConfigurationError(f"卫鼎模板响应非对象: {type(body)}")
    code = body.get("code")
    if code not in (0, "0", None) and body.get("success") is not True:
        raise TemplateConfigurationError(
            f"卫鼎模板业务失败 code={code} msg={body.get('msg')}"
        )
    data = body.get("data")
    if expect_list:
        if data is None:
            return []
        if not isinstance(data, list):
            raise TemplateConfigurationError("卫鼎模板 list.data 须为数组")
        return data
    if data is None:
        raise TemplateConfigurationError("卫鼎模板 detail.data 为空")
    if not isinstance(data, dict):
        raise TemplateConfigurationError("卫鼎模板 detail.data 须为对象")
    return data


def _parse_metafields(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clamp_tokens(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 1200
    return max(_MIN_OUTPUT_TOKENS, min(_MAX_OUTPUT_TOKENS, n))


def open_row_to_report_template(row: Dict[str, Any]) -> ReportTemplate:
    """将 AgentTemplateOpenOutSchema 映射为内部 ReportTemplate。"""
    tid = row.get("id")
    if tid is None or str(tid).strip() == "":
        raise TemplateConfigurationError("卫鼎模板缺少 id")
    template_id = str(tid).strip()
    name = str(row.get("name") or "").strip() or f"模板{template_id}"
    code = row.get("code")
    content = str(row.get("content") or "")
    meta = _parse_metafields(row.get("metafields"))

    guidance = str(meta.get("guidance") or "").strip()
    preview = str(meta.get("preview") or "").strip()
    generation = str(meta.get("generation") or "").strip()
    if not preview:
        preview = content
    if not generation:
        generation = content
    if not guidance:
        bits = [f"模板：{name}"]
        if code:
            bits.append(f"编码：{code}")
        guidance = "\n".join(bits)

    tags_raw = meta.get("tags")
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []

    return ReportTemplate(
        id=template_id,
        name=name,
        tagline=str(meta.get("tagline") or code or "").strip(),
        length_label=str(meta.get("length_label") or "").strip() or "按模板正文",
        tags=tags,
        recommended=bool(meta.get("recommended")),
        sort_order=int(meta.get("sort_order") or row.get("sort_order") or 0),
        max_output_tokens=_clamp_tokens(meta.get("max_output_tokens")),
        guidance_content=guidance,
        preview_content=preview,
        generation_content=generation,
    )


def fetch_open_template_list(
    *,
    timeout: Optional[float] = None,
) -> List[ReportTemplate]:
    base = _api_base()
    if not base:
        raise TemplateConfigurationError("未配置 AGENT_TEMPLATE_API_BASE_URL")
    url = f"{base}{_LIST_PATH}"
    t = float(timeout if timeout is not None else AGENT_TEMPLATE_TIMEOUT_SEC)
    # list 默认更短，避免占满线程池拖死整站；可用 AGENT_TEMPLATE_LIST_TIMEOUT_SEC 覆盖
    if timeout is None:
        list_override = os.getenv("AGENT_TEMPLATE_LIST_TIMEOUT_SEC")
        if list_override:
            t = float(list_override)
        else:
            t = min(t, 5.0)
    http_timeout = httpx.Timeout(t, connect=min(2.0, t))
    logger.info("agent-template open/list GET {}", url)
    with httpx.Client(timeout=http_timeout) as client:
        resp = client.get(url, headers=_headers())
        resp.raise_for_status()
        body = resp.json()
    rows = _unwrap_data(body, expect_list=True)
    out: List[ReportTemplate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(open_row_to_report_template(row))
        except TemplateConfigurationError as exc:
            logger.warning("跳过非法卫鼎模板行: {}", exc)
    out.sort(key=lambda x: (not x.recommended, x.sort_order, x.id))
    return out


def fetch_open_template_detail(
    template_id: Union[str, int],
    *,
    timeout: Optional[float] = None,
) -> ReportTemplate:
    base = _api_base()
    if not base:
        raise TemplateConfigurationError("未配置 AGENT_TEMPLATE_API_BASE_URL")
    tid = str(template_id).strip()
    if not tid:
        raise TemplateConfigurationError("模板 id 为空")
    # 卫鼎 path id 为 integer；非纯数字时仍尝试原样（兼容 code）
    path_id = tid
    url = f"{base}{_DETAIL_PATH.format(id=path_id)}"
    t = float(timeout if timeout is not None else AGENT_TEMPLATE_TIMEOUT_SEC)
    http_timeout = httpx.Timeout(t, connect=min(3.0, t))
    logger.info("agent-template open/detail GET {}", url)
    with httpx.Client(timeout=http_timeout) as client:
        resp = client.get(url, headers=_headers())
        resp.raise_for_status()
        body = resp.json()
    row = _unwrap_data(body, expect_list=False)
    return open_row_to_report_template(row)
