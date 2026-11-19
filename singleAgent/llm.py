"""singleAgent LLM 流式调用。"""

from __future__ import annotations

from datetime import datetime
import threading
import time
from typing import Any, Dict, Generator, Optional
from urllib.parse import urljoin

import httpx
from loguru import logger
from openai import APIConnectionError, APITimeoutError, OpenAI

from singleAgent.cancellation import GenerationCancelled, GenerationHandle
from singleAgent.config import (
    LLM_CONNECT_TIMEOUT_SEC,
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SEC,
    ROUTE_LLM_MAX_TOKENS,
    ROUTE_LLM_TEMPERATURE,
    ROUTE_LLM_TIMEOUT_SEC,
)
from singleAgent.diag_codes import (
    E2107_LLM_STREAM_CLOSE,
    E2108_LLM_CLIENT_CLOSE,
    format_diag,
    ms_bucket,
)


def resolve_llm_settings(
    config: Dict[str, Any],
    llm_override: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    override = llm_override or {}
    api_key = (
        override.get("api_key")
        or config.get("INSIGHT_ENGINE_API_KEY")
        or "EMPTY"
    )
    base_url = (
        override.get("base_url")
        or config.get("INSIGHT_ENGINE_BASE_URL")
        or ""
    )
    model_name = (
        override.get("model_name")
        or override.get("id")
        or config.get("INSIGHT_ENGINE_MODEL_NAME")
        or ""
    )
    if not model_name:
        raise ValueError("未配置 LLM 模型（model_config.id 或 INSIGHT_ENGINE_MODEL_NAME）")
    if not base_url:
        raise ValueError(
            "未配置 LLM 地址（model_config.base_url 或 INSIGHT_ENGINE_BASE_URL）"
        )
    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model_name": model_name,
    }


def _docker_network_hint(base_url: str) -> str:
    if any(x in base_url for x in ("127.0.0.1", "localhost", "10.", "192.168.", "172.")):
        return (
            " 若 API 跑在宿主机或内网，请确认 Docker 容器能访问该地址；"
            "宿主机服务可尝试 host.docker.internal 或宿主机网关 IP。"
        )
    return ""


def check_llm_connectivity(base_url: str, api_key: str, timeout: float) -> None:
    """连通性探测，失败时抛出带上下文的 RuntimeError。"""
    models_url = urljoin(f"{base_url.rstrip('/')}/", "models")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(models_url, headers=headers)
        if resp.status_code >= 500:
            raise RuntimeError(f"LLM 服务返回 {resp.status_code}（{models_url}）")
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"无法连接 LLM 服务：{base_url}{_docker_network_hint(base_url)}"
        ) from e
    except httpx.TimeoutException as e:
        raise RuntimeError(f"连接 LLM 服务超时（{base_url}，{timeout}s）") from e
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning(f"LLM 连通性探测异常（将继续尝试流式调用）: {e}")


def stream_chat(
    *,
    api_key: str,
    base_url: str,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = LLM_MAX_TOKENS,
    temperature: float = LLM_TEMPERATURE,
    timeout: float = LLM_TIMEOUT_SEC,
    connect_timeout: float = LLM_CONNECT_TIMEOUT_SEC,
    check_connectivity: bool = True,
    cancellation: Optional[GenerationHandle] = None,
) -> Generator[str, None, None]:
    """OpenAI 兼容流式生成。"""
    if cancellation:
        cancellation.raise_if_cancelled()
    if check_connectivity:
        check_llm_connectivity(base_url, api_key, connect_timeout)
    if cancellation:
        cancellation.raise_if_cancelled()

    current_time = datetime.now().strftime("%Y年%m月%d日%H时%M分")
    user_content = f"今天的实际时间是{current_time}\n{user_prompt}"

    client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
    stream = None
    close_lock = threading.Lock()
    stream_closed = False
    client_closed = False

    def close_upstream() -> None:
        """Close each upstream resource at most once, from any thread."""
        nonlocal stream_closed, client_closed
        started = time.time()
        req = cancellation.request_id if cancellation is not None else "-"
        with close_lock:
            current_stream = stream
            close_stream_now = current_stream is not None and not stream_closed
            close_client_now = not client_closed
            if close_stream_now:
                stream_closed = True
            if close_client_now:
                client_closed = True
        if close_stream_now:
            t_stream = time.time()
            close_stream = getattr(current_stream, "close", None)
            if callable(close_stream):
                try:
                    close_stream()
                    elapsed_ms = (time.time() - t_stream) * 1000
                    if elapsed_ms >= 500:
                        logger.warning(
                            format_diag(
                                E2107_LLM_STREAM_CLOSE,
                                "stream_close_ok",
                                request=req,
                                elapsed_ms=f"{elapsed_ms:.1f}",
                                bucket=ms_bucket(elapsed_ms),
                            )
                        )
                except Exception as exc:
                    elapsed_ms = (time.time() - t_stream) * 1000
                    logger.warning(
                        format_diag(
                            E2107_LLM_STREAM_CLOSE,
                            "stream_close_error",
                            request=req,
                            err_type=type(exc).__name__,
                            elapsed_ms=f"{elapsed_ms:.1f}",
                            bucket=ms_bucket(elapsed_ms),
                        )
                    )
        if close_client_now:
            t_client = time.time()
            close_client = getattr(client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                    elapsed_ms = (time.time() - t_client) * 1000
                    if elapsed_ms >= 500:
                        logger.warning(
                            format_diag(
                                E2108_LLM_CLIENT_CLOSE,
                                "client_close_ok",
                                request=req,
                                elapsed_ms=f"{elapsed_ms:.1f}",
                                bucket=ms_bucket(elapsed_ms),
                            )
                        )
                except Exception as exc:
                    elapsed_ms = (time.time() - t_client) * 1000
                    logger.warning(
                        format_diag(
                            E2108_LLM_CLIENT_CLOSE,
                            "client_close_error",
                            request=req,
                            err_type=type(exc).__name__,
                            elapsed_ms=f"{elapsed_ms:.1f}",
                            bucket=ms_bucket(elapsed_ms),
                        )
                    )
        total_ms = (time.time() - started) * 1000
        if total_ms >= 500:
            logger.warning(
                format_diag(
                    E2107_LLM_STREAM_CLOSE if close_stream_now else E2108_LLM_CLIENT_CLOSE,
                    "close_upstream_total",
                    request=req,
                    elapsed_ms=f"{total_ms:.1f}",
                    bucket=ms_bucket(total_ms),
                    closed_stream=close_stream_now,
                    closed_client=close_client_now,
                )
            )

    closer_token = (
        cancellation.register_closer(close_upstream)
        if cancellation is not None
        else None
    )

    try:
        if cancellation:
            cancellation.raise_if_cancelled()
        stream = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if cancellation:
            cancellation.raise_if_cancelled()
    except APIConnectionError as e:
        if cancellation is not None and closer_token is not None:
            cancellation.unregister_closer(closer_token)
        close_upstream()
        if cancellation and cancellation.cancelled:
            raise GenerationCancelled(cancellation.cancel_reason or "user") from e
        raise RuntimeError(
            f"无法连接 LLM（{base_url}，模型 {model_name}）"
            f"{_docker_network_hint(base_url)}"
        ) from e
    except APITimeoutError as e:
        if cancellation is not None and closer_token is not None:
            cancellation.unregister_closer(closer_token)
        close_upstream()
        if cancellation and cancellation.cancelled:
            raise GenerationCancelled(cancellation.cancel_reason or "user") from e
        raise RuntimeError(f"LLM 请求超时（{base_url}，模型 {model_name}）") from e
    except Exception as e:
        if cancellation is not None and closer_token is not None:
            cancellation.unregister_closer(closer_token)
        close_upstream()
        if cancellation and cancellation.cancelled:
            raise GenerationCancelled(cancellation.cancel_reason or "user") from e
        raise

    try:
        for chunk in stream:
            if cancellation:
                cancellation.raise_if_cancelled()
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content
            if cancellation:
                cancellation.raise_if_cancelled()
    except GenerationCancelled:
        raise
    except Exception as e:
        if cancellation and cancellation.cancelled:
            raise GenerationCancelled(cancellation.cancel_reason or "user") from e
        raise
    finally:
        if cancellation is not None and closer_token is not None:
            cancellation.unregister_closer(closer_token)
        close_upstream()


def complete_chat(
    *,
    api_key: str,
    base_url: str,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = ROUTE_LLM_MAX_TOKENS,
    temperature: float = ROUTE_LLM_TEMPERATURE,
    timeout: float = ROUTE_LLM_TIMEOUT_SEC,
    connect_timeout: float = LLM_CONNECT_TIMEOUT_SEC,
    check_connectivity: bool = True,
) -> str:
    """OpenAI 兼容非流式调用（用于意图分类等短回复）。"""
    if check_connectivity:
        check_llm_connectivity(base_url, api_key, connect_timeout)

    client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    try:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except APIConnectionError as e:
        raise RuntimeError(
            f"无法连接 LLM（{base_url}，模型 {model_name}）"
            f"{_docker_network_hint(base_url)}"
        ) from e
    except APITimeoutError as e:
        raise RuntimeError(f"LLM 请求超时（{base_url}，模型 {model_name}）") from e

    if not resp.choices:
        return ""
    return (resp.choices[0].message.content or "").strip()

