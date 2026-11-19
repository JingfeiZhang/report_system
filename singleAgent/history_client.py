"""会话历史：按条追加到 Agent Open API。

POST /api/v1/ai/session/open/message
- agent_id：后端固定配置
- session_id：前端传入
- role：human / ai
- content / metadata
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from loguru import logger

_MESSAGE_PATH = "/api/v1/ai/session/open/message"


def _get_setting(name: str, default: Any = None) -> Any:
    try:
        from config import settings

        value = getattr(settings, name, None)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return os.getenv(name, default)


@dataclass
class SessionHistoryConfig:
    base_url: str = ""
    bearer_token: str = ""
    agent_id: int = 5
    timeout_sec: float = 8.0


class SessionHistoryClient:
    """调用「更新会话记录-Agent」接口，按消息追加。"""

    def __init__(self, config: SessionHistoryConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls) -> "SessionHistoryClient":
        base_url = (
            _get_setting("SESSION_HISTORY_API_BASE_URL")
            or _get_setting("KNOWLEDGE_BASE_API_BASE_URL")
            or _get_setting("DATAPLATFORM_BASE_URL")
            or ""
        )
        bearer_token = (
            _get_setting("SESSION_HISTORY_API_KEY")
            or _get_setting("KNOWLEDGE_BASE_API_KEY")
            or _get_setting("DATAPLATFORM_BEARER_TOKEN")
            or ""
        )
        try:
            agent_id = int(
                _get_setting("SESSION_HISTORY_AGENT_ID")
                or os.getenv("SESSION_HISTORY_AGENT_ID", "5")
            )
        except (TypeError, ValueError):
            agent_id = 5
        try:
            timeout_sec = float(
                _get_setting("SESSION_HISTORY_TIMEOUT_SEC")
                or _get_setting("KNOWLEDGE_BASE_TIMEOUT_SEC")
                or 8
            )
        except (TypeError, ValueError):
            timeout_sec = 8.0

        return cls(
            SessionHistoryConfig(
                base_url=str(base_url).rstrip("/"),
                bearer_token=str(bearer_token).strip(),
                agent_id=agent_id,
                timeout_sec=timeout_sec,
            )
        )

    @property
    def available(self) -> bool:
        return bool(self.config.base_url and self.config.bearer_token)

    @property
    def agent_id(self) -> int:
        return self.config.agent_id

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """追加一条会话消息。失败仅打日志并返回 None，不抛给主流程。"""
        sid = (session_id or "").strip()
        text = (content or "").strip()
        platform_role = _to_platform_role(role)
        if not sid or not text:
            return None
        if not self.available:
            logger.debug("会话历史接口未配置，跳过平台写入")
            return None

        body = {
            "agent_id": self.config.agent_id,
            "session_id": sid,
            "role": platform_role,
            "content": text,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        url = f"{self.config.base_url}{_MESSAGE_PATH}"
        try:
            with httpx.Client(timeout=self.config.timeout_sec) as client:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.config.bearer_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=body,
                )
            if resp.status_code >= 400:
                logger.warning(
                    f"会话历史追加失败 HTTP {resp.status_code} session={sid} role={platform_role}: "
                    f"{resp.text[:300]}"
                )
                return None
            data = resp.json()
            code = data.get("code") if isinstance(data, dict) else None
            if code not in (None, 0, "0"):
                logger.warning(
                    f"会话历史追加失败 session={sid} role={platform_role}: "
                    f"{data.get('msg') or data}"
                )
                return None
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"会话历史追加异常 session={sid} role={platform_role}: {e}")
            return None


# 兼容旧导入名（若有外部引用）
HistoryDataClient = SessionHistoryClient


def _to_platform_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("human", "user"):
        return "human"
    if r in ("ai", "assistant"):
        return "ai"
    return r or "ai"
