"""本地验证 7-31 三接口 mock（默认打 http://127.0.0.1:8000；ASGI 不依赖实机）。"""

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


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


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


def test_offline_builders() -> None:
    import os

    # 严肃模式：未知 ID 仍为空骨架（演示模式另测）
    os.environ["EVENT_REPORT_DEMO_MODE"] = "false"

    from event_report.impact import build_impact_data
    from event_report.open_events import build_open_events_data
    from event_report.sensitive_analysis import build_sensitive_analysis_data
    from event_report.dm_event_stats import heat_trend_from_daily

    # 达梦热度结构冒烟（不连库）
    trend, _, heat = heat_trend_from_daily(
        "2026-08-06",
        {f"2026-08-{d:02d}": 10 * d for d in range(1, 10)},
        before=5,
        after=3,
    )
    _assert(len(trend) == 9, "dm trend 9")
    _assert(heat["total"] == 60, "dm event day heat")

    impact = build_impact_data(171114)
    _assert(impact["main_event_id"] == 171114, "impact 样例 ID")
    _assert(impact["word_cloud"], "impact 词云")

    page = build_open_events_data(page=1, page_size=50)
    _assert(page["all"] is False and page["page"] == 1, "open 分页字段")
    _assert(page["start_date"] == "2026-08-06" and page["end_date"] == "2026-08-20", "默认窗")
    _assert(page["total"] >= 5, "open 默认窗条数")
    _assert(any(i.get("id") == 171114 for i in page["items"]), "含 171114")
    for i in page["items"]:
        _assert("2026-08-06" <= i["event_date"] <= "2026-08-20", f"默认窗日期 {i}")
        if i["is_sensitive"]:
            _assert(i["event_type"] == "record", f"敏感须 record {i}")
            _assert(i["id"] == i.get("record_event_id"), f"敏感 id=record {i}")
            _assert(i.get("main_event_id") is None, f"敏感 main 须 null {i}")
        else:
            _assert(i["event_type"] == "monitor", f"非敏感须 monitor {i}")
            _assert(i["id"] == i.get("main_event_id"), f"非敏感 id=main {i}")
            _assert(i.get("record_event_id") is None, f"非敏感 record 须 null {i}")

    # 扩窗可见旧样例 101（敏感 → record_event_id）
    wide = build_open_events_data(
        start_date="2024-01-01", end_date="2026-12-31", page_size=100
    )
    _assert(any(i.get("record_event_id") == 101 for i in wide["items"]), "扩窗含 101")

    sens = build_open_events_data(is_sensitive=True)
    _assert(all(i["is_sensitive"] for i in sens["items"]), "is_sensitive=true")
    _assert(sens["is_sensitive"] is True, "回显 is_sensitive")
    _assert(all(i.get("main_event_id") is None for i in sens["items"]), "敏感无 main")
    _assert(all(i["id"] == i.get("record_event_id") for i in sens["items"]), "敏感 id=record")
    _assert(all(i["event_type"] == "record" for i in sens["items"]), "敏感 event_type=record")

    non_sens = build_open_events_data(is_sensitive=False)
    _assert(all(not i["is_sensitive"] for i in non_sens["items"]), "is_sensitive=false")
    _assert(any(i.get("id") == 20260818 for i in non_sens["items"]), "窗内非敏感对照")
    _assert(all(i.get("record_event_id") is None for i in non_sens["items"]), "非敏感无 record")
    _assert(all(i["id"] == i.get("main_event_id") for i in non_sens["items"]), "非敏感 id=main")
    _assert(all(i["event_type"] == "monitor" for i in non_sens["items"]), "非敏感 event_type=monitor")

    sa = build_sensitive_analysis_data(171114)
    _assert(sa["heat"]["total"] == 8642, "sensitive heat")
    _assert(sa["event_type"] == "monitor", "缺省 monitor")
    _assert(sa["main_event_id"] == 171114, "monitor main_event_id")
    _assert(sa["record_event_id"] is None, "monitor 无 record_event_id")
    _assert(len(sa["heat_trend"]) == 9, "趋势 9 点")
    _assert(len(sa["self_media_opinions"]) <= 3, "观点最多 3")

    sa_new = build_sensitive_analysis_data(20260811)
    _assert(sa_new["heat"]["total"] > 0, "南海样例 heat")
    _assert(sa_new["event_date"] == "2026-08-11", sa_new)

    impact_new = build_impact_data(20260807)
    _assert(impact_new["word_cloud"], "麦加样例 impact")

    sa_m = build_sensitive_analysis_data(171114, event_type="monitor")
    _assert(sa_m["event_type"] == "monitor" and sa_m["main_event_id"] == 171114, sa_m)

    sa_r = build_sensitive_analysis_data(1, event_type="record")
    _assert(sa_r["event_type"] == "record", sa_r)
    _assert(sa_r["main_event_id"] is None, "record 线 main_event_id 须 null")
    _assert(sa_r["record_event_id"] == 1, sa_r)

    # 敏感样例已归 record 线：按 record 取 171114 / 20260815 须有热度
    sa_r_sens = build_sensitive_analysis_data(171114, event_type="record")
    _assert(sa_r_sens["event_type"] == "record", sa_r_sens)
    _assert(sa_r_sens["main_event_id"] is None, sa_r_sens)
    _assert(sa_r_sens["record_event_id"] == 171114, sa_r_sens)
    _assert(sa_r_sens["heat"]["total"] == 8642, "record 线应命中敏感样例热度")

    sa_r_0815 = build_sensitive_analysis_data(20260815, event_type="record")
    _assert(sa_r_0815["heat"]["total"] > 0 and sa_r_0815["event_date"] == "2026-08-15", sa_r_0815)

    # 未知 record id 仍空骨架
    sa_r_unknown = build_sensitive_analysis_data(999001, event_type="record")
    _assert(sa_r_unknown["heat"]["total"] == 0, "未知 record 空热度")
    _assert(sa_r_unknown["record_event_id"] == 999001, sa_r_unknown)

    empty = build_sensitive_analysis_data(999999)
    _assert(empty["heat"]["change_direction"] == "unknown", "未知 ID 空骨架")
    _assert(empty["heat_trend"] == [], "未知趋势空")
    _assert(empty["event_type"] == "monitor" and empty["record_event_id"] is None, empty)
    print("[ok] offline builders")


def test_asgi() -> None:
    import os

    os.environ["OPEN_EVENTS_SOURCE"] = "mock"
    os.environ["EVENT_REPORT_DEMO_MODE"] = "false"

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from event_report import router as event_report_router

    app = FastAPI()
    app.include_router(event_report_router)
    client = TestClient(app)

    r = client.get("/api/v1/open/events")
    _assert(r.status_code == 200, r.text)
    body = r.json()
    _assert(body["code"] == 0, body)
    data = body["data"]
    _assert(data["page"] == 1 and data["page_size"] == 50, data)
    _assert(data["is_sensitive"] is None, "未传 is_sensitive 回显 null")
    _assert(data.get("start_date") == "2026-08-06", data)
    _assert(data.get("end_date") == "2026-08-20", data)
    _assert(len(data["items"]) > 0, "items")
    hit_171114 = next(i for i in data["items"] if i.get("id") == 171114)
    _assert(hit_171114["is_sensitive"] is True, hit_171114)
    _assert(hit_171114["event_type"] == "record", hit_171114)
    _assert(hit_171114.get("record_event_id") == 171114, hit_171114)
    _assert(hit_171114.get("main_event_id") is None, hit_171114)

    r = client.get("/api/v1/open/events", params={"is_sensitive": "true", "page_size": 20})
    _assert(r.status_code == 200 and r.json()["data"]["is_sensitive"] is True, r.text)
    _assert(all(i["is_sensitive"] for i in r.json()["data"]["items"]), "敏感筛选")

    r = client.get("/api/v1/open/events", params={"all": "true"})
    _assert(r.status_code == 200, r.text)
    d = r.json()["data"]
    _assert(d["all"] is True and d["max_limit"] == 1000, d)
    _assert("page" not in d, "全量无 page")

    r = client.get("/api/v1/open/events", params={"start_date": "2024-01-01"})
    _assert(r.status_code == 400, f"日期不成对应 400: {r.text}")

    r = client.get("/api/v1/main-events/171114/sensitive-analysis")
    _assert(r.status_code == 200 and r.json()["code"] == 0, r.text)
    sa = r.json()["data"]
    _assert(sa["main_event_id"] == 171114, sa)
    _assert(sa["event_type"] == "monitor", sa)
    _assert(sa["record_event_id"] is None, sa)
    _assert(sa["heat"]["total"] > 0, sa)
    _assert(len(sa["self_media_opinions"]) == 3, sa)

    r = client.get(
        "/api/v1/main-events/171114/sensitive-analysis",
        params={"event_type": "monitor"},
    )
    _assert(r.status_code == 200 and r.json()["data"]["event_type"] == "monitor", r.text)

    r = client.get(
        "/api/v1/main-events/1/sensitive-analysis",
        params={"event_type": "record"},
    )
    _assert(r.status_code == 200 and r.json()["code"] == 0, r.text)
    sa_r = r.json()["data"]
    _assert(sa_r["event_type"] == "record", sa_r)
    _assert(sa_r["main_event_id"] is None, sa_r)
    _assert(sa_r["record_event_id"] == 1, sa_r)

    r = client.get(
        "/api/v1/main-events/1/sensitive-analysis",
        params={"event_type": "foo"},
    )
    _assert(r.status_code == 422, f"非法 event_type 应 422: {r.text}")

    r = client.get("/api/v1/main-events/999999/sensitive-analysis")
    _assert(r.status_code == 200 and r.json()["code"] == 0, r.text)
    _assert(r.json()["data"]["heat_trend"] == [], "空骨架")
    _assert(r.json()["data"]["event_type"] == "monitor", r.text)

    r = client.get("/api/v1/event-report/impact", params={"main_event_id": "171114"})
    _assert(r.status_code == 200 and r.json()["code"] == 0, r.text)
    _assert(r.json()["data"]["word_cloud"], "impact 仍可用")
    print("[ok] ASGI TestClient")


def test_http() -> None:
    status, body = _get("/api/v1/open/events?page=1&page_size=10")
    _assert(status == 200 and body.get("code") == 0, body)
    _assert(isinstance((body.get("data") or {}).get("items"), list), body)
    print("[ok] http open/events")

    status, body = _get("/api/v1/main-events/171114/sensitive-analysis?event_type=monitor")
    _assert(status == 200 and body.get("code") == 0, body)
    _assert((body.get("data") or {}).get("event_type") == "monitor", body)
    _assert((body.get("data") or {}).get("heat", {}).get("total", 0) > 0, body)
    print("[ok] http sensitive-analysis monitor")

    status, body = _get("/api/v1/main-events/1/sensitive-analysis?event_type=record")
    _assert(status == 200 and body.get("code") == 0, body)
    d = body.get("data") or {}
    _assert(d.get("event_type") == "record", body)
    _assert(d.get("main_event_id") is None and d.get("record_event_id") == 1, body)
    print("[ok] http sensitive-analysis record")

    q = urllib.parse.urlencode({"main_event_id": "171114"})
    status, body = _get(f"/api/v1/event-report/impact?{q}")
    _assert(status == 200 and body.get("code") == 0, body)
    print("[ok] http impact")


def main() -> int:
    test_offline_builders()
    test_asgi()
    try:
        test_http()
    except urllib.error.URLError as e:
        print(f"[skip] 实机 HTTP 跳过（8000 未监听）: {e}")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
