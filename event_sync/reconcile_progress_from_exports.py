#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据结果目录把对应事件标为 completed。

优先读分级路径：
  exports/event_sync/{YYYY}/{MM}/{DD}/{YYYYMMDD}-{id}/
兼容旧扁平路径：
  exports/event_sync/{YYYYMMDD}-{id}/

用法（在 event_sync 目录）:
  python reconcile_progress_from_exports.py --day 2026-08-09
  python reconcile_progress_from_exports.py --day 2026-08-09 --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def load_config(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def _collect_ids(exports: Path, compact: str, day: str) -> list[int]:
    """从分级目录与旧扁平目录收集事件 id。"""
    pat = re.compile(rf"^{re.escape(compact)}-(\d+)$")
    ids: list[int] = []
    seen: set[int] = set()

    def _add_from(parent: Path) -> None:
        if not parent.is_dir():
            return
        for d in sorted(parent.iterdir()):
            if not d.is_dir():
                continue
            m = pat.match(d.name)
            if not m:
                continue
            eid = int(m.group(1))
            if eid not in seen:
                seen.add(eid)
                ids.append(eid)

    # 新结构：年/月/日/
    parts = day.split("-")
    if len(parts) == 3:
        _add_from(exports / parts[0] / parts[1] / parts[2])

    # 旧结构：根目录扁平
    _add_from(exports)
    return ids


def main() -> int:
    from progress_reporter import mark_ids, progress_enabled

    p = argparse.ArgumentParser(description="按结果文件夹回写 completed")
    p.add_argument("--day", required=True, help="采集日 YYYY-MM-DD")
    p.add_argument("--config", default=str(HERE / "config.txt"))
    p.add_argument(
        "--exports",
        default="",
        help="结果根目录，默认读 config CRAWLER_DOWNLOAD_DIR",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    config = load_config(Path(args.config))
    # 对账时强制开启进度回写
    config["OPINION_PROGRESS_ENABLED"] = "true"

    day = args.day.strip()
    compact = day.replace("-", "")
    exports = Path(args.exports) if args.exports else Path(
        config.get("CRAWLER_DOWNLOAD_DIR") or (HERE.parent / "exports" / "event_sync")
    )
    if not exports.is_absolute():
        exports = (HERE / exports).resolve()

    ids = _collect_ids(exports, compact, day)

    print(f"day={day} exports={exports} folders={len(ids)}")
    if not ids:
        print("无匹配目录，退出")
        return 1
    print("ids=", ids[:20], ("..." if len(ids) > 20 else ""))

    if args.dry_run:
        print("dry-run：不调用 mark-ids")
        return 0

    if not progress_enabled(config):
        print("进度未启用")
        return 2

    data = mark_ids(
        config,
        day,
        ids,
        status="completed",
        message="reconcile from export folders",
    )
    if not data:
        print("mark-ids 失败（请确认能访问 OPINION_PROGRESS_BASE）")
        return 3
    print("progress=", data.get("progress") or data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
