"""本地验证 GET /api/v1/event-report/impact（默认打 http://127.0.0.1:8000）。"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"


def _get(path: str) -> tuple[int, dict]:
    url = BASE.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return e.code, body


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_offline_builder() -> None:
    from event_report.impact import build_impact_data

    sample = build_impact_data(171114)
    _assert(sample["main_event_id"] == 171114, "样例 ID 回传")
    _assert(isinstance(sample["word_cloud"], list) and sample["word_cloud"], "样例应有词云")
    _assert("domestic" in sample["media_opinions"], "应有 domestic")
    _assert("international" in sample["media_opinions"], "应有 international")

    unknown = build_impact_data(999999)
    _assert(unknown["casualties"] == "暂无权威伤亡数据", "未知 ID 默认伤亡文案")
    _assert(unknown["word_cloud"] == [], "未知 ID 词云为空列表")
    print("[ok] offline builder")


def test_asgi() -> None:
    """不启独立进程：把同一 router 挂到临时 app 上测。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from event_report import router as event_report_router

    app = FastAPI()
    app.include_router(event_report_router)
    client = TestClient(app)

    r = client.get("/api/v1/event-report/impact")
    _assert(r.status_code == 422, f"缺参应 422，got {r.status_code}: {r.text}")

    r = client.get("/api/v1/event-report/impact", params={"main_event_id": "171114"})
    _assert(r.status_code == 200, f"样例应 200: {r.text}")
    body = r.json()
    _assert(body.get("code") == 0, f"code: {body}")
    data = body.get("data") or {}
    _assert(data.get("main_event_id") in (171114, "171114"), "main_event_id")
    _assert(len(data.get("word_cloud") or []) > 0, "word_cloud")
    print("[ok] ASGI TestClient sample")

    r = client.get("/api/v1/event-report/impact", params={"main_event_id": "999999"})
    _assert(r.status_code == 200 and r.json().get("code") == 0, r.text)
    _assert((r.json().get("data") or {}).get("impact_scope") == "暂无", "骨架")
    print("[ok] ASGI TestClient unknown id")


def test_http() -> None:
    # 缺参
    status, body = _get("/api/v1/event-report/impact")
    _assert(status == 422, f"缺参应 422，got {status}: {body}")

    # 样例
    q = urllib.parse.urlencode({"main_event_id": "171114"})
    status, body = _get(f"/api/v1/event-report/impact?{q}")
    _assert(status == 200, f"样例应 200，got {status}: {body}")
    _assert(body.get("code") == 0, f"code 应为 0: {body}")
    data = body.get("data") or {}
    _assert(data.get("main_event_id") in (171114, "171114"), "main_event_id 回传")
    _assert(isinstance(data.get("word_cloud"), list) and len(data["word_cloud"]) > 0, "word_cloud")
    _assert(isinstance((data.get("media_opinions") or {}).get("domestic"), list), "domestic")
    print("[ok] http sample 171114")

    # 未知 ID
    q2 = urllib.parse.urlencode({"main_event_id": "999999"})
    status, body = _get(f"/api/v1/event-report/impact?{q2}")
    _assert(status == 200 and body.get("code") == 0, f"未知 ID 仍应成功骨架: {body}")
    _assert((body.get("data") or {}).get("impact_scope") == "暂无", "骨架 impact_scope")
    print("[ok] http unknown id skeleton")


def main() -> int:
    test_offline_builder()
    try:
        test_asgi()
    except Exception as e:
        print(f"[warn] ASGI 测试失败: {e}")
        raise
    try:
        test_http()
    except urllib.error.URLError as e:
        print(f"[skip] 实机 HTTP 跳过（8000 未监听）: {e}")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
