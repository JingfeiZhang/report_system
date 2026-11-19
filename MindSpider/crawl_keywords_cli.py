#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按关键词驱动 MediaCrawler（供厂家 opinion_crawl 调用）。

用法（在 MindSpider 目录或其上级均可，脚本会修正 path）：
  python crawl_keywords_cli.py --keywords 基辅遭袭 青岭港 --platforms wb --max-notes 20 --lt cookie
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DSC = _HERE / "DeepSentimentCrawling"
for p in (_HERE, _DSC):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)


def main() -> int:
    parser = argparse.ArgumentParser(description="MindSpider 关键词爬取 CLI")
    parser.add_argument("--keywords", nargs="+", required=True, help="关键词列表")
    parser.add_argument(
        "--platforms",
        nargs="+",
        default=["wb"],
        help="平台：xhs dy ks bili wb tieba zhihu",
    )
    parser.add_argument("--max-notes", type=int, default=20)
    parser.add_argument(
        "--lt",
        default="cookie",
        choices=["qrcode", "phone", "cookie"],
        help="登录方式（服务端建议 cookie）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验导入与参数，不真正启动爬虫",
    )
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords if k and str(k).strip()]
    platforms = [p.strip() for p in args.platforms if p and str(p).strip()]
    if not keywords:
        print(json.dumps({"success": False, "error": "keywords empty"}, ensure_ascii=False))
        return 2
    if not platforms:
        print(json.dumps({"success": False, "error": "platforms empty"}, ensure_ascii=False))
        return 2

    if args.dry_run:
        print(
            json.dumps(
                {
                    "success": True,
                    "dry_run": True,
                    "keywords": keywords,
                    "platforms": platforms,
                    "max_notes": args.max_notes,
                    "login_type": args.lt,
                },
                ensure_ascii=False,
            )
        )
        return 0

    from platform_crawler import PlatformCrawler  # noqa: WPS433

    crawler = PlatformCrawler()
    result = crawler.run_multi_platform_crawl_by_keywords(
        keywords,
        platforms,
        login_type=args.lt,
        max_notes_per_keyword=args.max_notes,
    )
    out = dict(result) if isinstance(result, dict) else {"raw": str(result)}
    successful = int(out.get("successful_tasks") or 0)
    out["success"] = successful > 0 or bool(out.get("success"))
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
