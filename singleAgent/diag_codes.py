"""event2 内场诊断编号（可出口报码，不含业务正文）。

内场人员只需口述/写下编号序列，例如：E2103 → E2105 → E2106 → E2201
完整释义见 docs/EVENT2_DIAG_CODES.md
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# ----- E21xx：stop / cancel / LLM closer -----
E2101_STOP_BEGIN = "E2101"
E2102_STOP_NOT_FOUND = "E2102"
E2103_CANCEL_ACCEPTED = "E2103"
E2104_CLOSER_DONE = "E2104"
E2105_CLOSER_TIMEOUT = "E2105"
E2106_STOP_SLOW = "E2106"
E2107_LLM_STREAM_CLOSE = "E2107"
E2108_LLM_CLIENT_CLOSE = "E2108"
E2109_STOP_END = "E2109"
E2110_STOP_PERSIST = "E2110"

# ----- E22xx：/health -----
E2201_HEALTH_SLOW = "E2201"
E2202_HEALTH_MOCK_FAIL = "E2202"

# ----- E23xx：落库 -----
E2301_PERSIST_ERROR = "E2301"

# ----- E29xx：汇总 -----
E2901_LOOP_BLOCK_SUSPECT = "E2901"

CODE_TITLES = {
    E2101_STOP_BEGIN: "stop 开始",
    E2102_STOP_NOT_FOUND: "stop 无活跃任务",
    E2103_CANCEL_ACCEPTED: "cancel 已接受，准备关上游",
    E2104_CLOSER_DONE: "单个 closer 结束",
    E2105_CLOSER_TIMEOUT: "closer 超时仍在后台",
    E2106_STOP_SLOW: "stop 总耗时过长",
    E2107_LLM_STREAM_CLOSE: "LLM stream.close 慢或失败",
    E2108_LLM_CLIENT_CLOSE: "LLM client.close 慢或失败",
    E2109_STOP_END: "stop 结束",
    E2110_STOP_PERSIST: "取消落库结果",
    E2201_HEALTH_SLOW: "/health 慢",
    E2202_HEALTH_MOCK_FAIL: "mock 探活失败/超时",
    E2301_PERSIST_ERROR: "取消落库失败",
    E2901_LOOP_BLOCK_SUSPECT: "疑似事件循环被堵（综合）",
}


def format_diag(code: str, detail: str = "", **fields: Any) -> str:
    """统一日志前缀：E2105 closer_timeout ms=2031 ..."""
    title = CODE_TITLES.get(code, "")
    parts = [code]
    if title:
        parts.append(title)
    if detail:
        parts.append(detail)
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(str(p) for p in parts)


def ms_bucket(elapsed_ms: float) -> str:
    if elapsed_ms < 100:
        return "lt100ms"
    if elapsed_ms < 2000:
        return "100ms_2s"
    return "gt2s"
