#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取 open/events 指定日期区间并落盘，供离线续爬。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from api_client import fetch_from_api  # noqa: E402


def load_config(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def main() -> int:
    start = "2026-08-01"
    end = "2026-09-01"
    cfg = load_config(HERE / "config.txt")
    print("API_BASE=", cfg.get("API_BASE"))
    items = fetch_from_api(cfg, start_date=start, end_date=end, page_size=200)
    print("fetched", len(items))

    tag = f"{start.replace('-', '')}_{end.replace('-', '')}"
    out = HERE / f"events_{tag}.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    by_date = Counter(str(e.get("event_date") or "")[:10] for e in items)
    meta = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": (cfg.get("API_BASE") or "").rstrip("/") + "/open/events",
        "start_date": start,
        "end_date": end,
        "total": len(items),
        "by_date": dict(sorted(by_date.items())),
        "events_file": out.name,
        "crawl_hint": (
            f"python main.py --from-file {out.name} "
            f"--start-date {start} --end-date {end}"
        ),
    }
    meta_path = HERE / f"events_{tag}_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("wrote", out)
    print("wrote", meta_path)
    print("days=", len(by_date), "total=", len(items))
    sample = [e.get("id") or e.get("main_event_id") for e in items[:8]]
    print("sample ids=", sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
