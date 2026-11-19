#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把旧扁平目录 exports/{YYYYMMDD}-{id} 迁到 年/月/日/ 下。

用法（在 event_sync 目录）:
  python migrate_exports_hierarchy.py
  python migrate_exports_hierarchy.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAT = re.compile(r"^(\d{8})-(\d+)$")


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
    p = argparse.ArgumentParser(description="扁平结果目录 → 年/月/日 分级")
    p.add_argument("--config", default=str(HERE / "config.txt"))
    p.add_argument("--exports", default="")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    config = load_config(Path(args.config))
    exports = Path(args.exports) if args.exports else Path(
        config.get("CRAWLER_DOWNLOAD_DIR") or (HERE.parent / "exports" / "event_sync")
    )
    if not exports.is_absolute():
        exports = (HERE / exports).resolve()

    if not exports.is_dir():
        print(f"目录不存在: {exports}")
        return 1

    moved = 0
    skipped = 0
    for d in sorted(exports.iterdir()):
        if not d.is_dir():
            continue
        m = PAT.match(d.name)
        if not m:
            continue
        compact = m.group(1)
        yyyy, mm, dd = compact[:4], compact[4:6], compact[6:8]
        dest = exports / yyyy / mm / dd / d.name
        if dest.exists():
            print(f"跳过(目标已存在): {d.name} -> {dest}")
            skipped += 1
            continue
        print(f"{'DRY ' if args.dry_run else ''}MOVE {d} -> {dest}")
        if not args.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dest))
        moved += 1

    print(f"done moved={moved} skipped={skipped} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
