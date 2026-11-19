#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件同步辅助程序入口：拉事件 → 写 daily_topics →（可选）启爬虫。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from api_client import (
    fetch_from_api,
    fetch_from_file,
    group_events_by_date,
    group_titles_by_date,
)
from crawler_launcher import launch_crawlers
from progress_reporter import progress_enabled, report_day, sync_items

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
DEFAULT_CONFIG = HERE / "config.txt"

logger = logging.getLogger("event_sync.main")


def _truthy(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def setup_logging() -> Path:
    """日志同时输出到控制台与 logs/event_sync_YYYYMMDD.log。"""
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"event_sync_{datetime.now():%Y%m%d}.log"

    root_logger = logging.getLogger("event_sync")
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    root_logger.addHandler(fh)
    root_logger.addHandler(sh)
    return log_file


def load_config(path: str | Path) -> dict:
    cfg = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="对外开放事件 → daily_topics → 爬虫")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    p.add_argument("--from-file", help="本地事件 JSON（无接口时使用）")
    p.add_argument("--start-date", help="事件起始日 YYYY-MM-DD")
    p.add_argument("--end-date", help="事件结束日 YYYY-MM-DD")
    p.add_argument("--topic-id", help="专题过滤")
    p.add_argument("--region", choices=["domestic", "foreign"], help="区域过滤")
    p.add_argument("--keyword", help="标题/摘要模糊搜索")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只拉取并打印分组结果，不写库、不爬虫",
    )
    return p.parse_args()


def main() -> int:
    log_file = setup_logging()
    args = parse_args()
    config = load_config(args.config)

    logger.info("日志文件: %s", log_file)
    logger.info(
        "配置 DB=%s dialect=%s CRAWLER_ENABLED=%s CRAWLER_BACKEND=%s",
        config.get("DB_NAME"),
        config.get("DB_DIALECT"),
        config.get("CRAWLER_ENABLED"),
        config.get("CRAWLER_BACKEND", "cli"),
    )

    if not args.from_file and (args.start_date is None) ^ (args.end_date is None):
        logger.error("--start-date 与 --end-date 需同时传或同时省略")
        return 2

    raw_events: list = []
    try:
        if args.from_file:
            raw_events = fetch_from_file(args.from_file)
        else:
            raw_events = fetch_from_api(
                config,
                start_date=args.start_date,
                end_date=args.end_date,
                topic_id=args.topic_id,
                region=args.region,
                keyword=args.keyword,
            )
        grouped = group_titles_by_date(raw_events)
        events_by_date = group_events_by_date(raw_events)
    except Exception:
        logger.exception("拉取事件失败")
        return 1

    total_titles = sum(len(v) for v in grouped.values())
    logger.info("拉取结果: 日期数=%d 标题数=%d", len(grouped), total_titles)
    for d, titles in sorted(grouped.items()):
        logger.info("  %s: %s", d, titles)

    if not grouped:
        logger.info("无事件可写，结束")
        return 0

    if args.dry_run:
        logger.info("dry-run：跳过写库与爬虫")
        return 0

    write_topics = _truthy(config.get("WRITE_DAILY_TOPICS", "true"))
    if write_topics:
        try:
            from db_writer import DailyTopicsWriter
        except ImportError as e:
            logger.error(
                "写库需要 sqlalchemy 等依赖: %s；可 pip install -r requirements.txt，"
                "或设 WRITE_DAILY_TOPICS=false（http 直爬）",
                e,
            )
            return 1
        try:
            with DailyTopicsWriter(config) as writer:
                ok_dates = writer.upsert_grouped_titles(grouped)
        except Exception:
            logger.exception("写入 daily_topics 失败")
            return 1
        logger.info("成功写入/更新日期: %s", ok_dates)
    else:
        ok_dates = sorted(grouped.keys())
        logger.info("WRITE_DAILY_TOPICS=false，跳过写库；将用事件标题直接爬取: %s", ok_dates)

    # 采集日：起止同一天 → 该日；多日范围 → 按各 event_date 落盘，进度按日 sync
    crawl_day = None
    multi_day = False
    if args.start_date and args.end_date and args.start_date == args.end_date:
        crawl_day = args.start_date
    elif args.start_date and args.end_date and args.start_date != args.end_date:
        multi_day = True
    elif (config.get("OPINION_CRAWL_DAY") or "").strip():
        crawl_day = config["OPINION_CRAWL_DAY"].strip()

    if crawl_day and not multi_day:
        config["_CRAWL_DAY"] = crawl_day
        # 单日批次：文件夹前缀用采集日 YYYYMMDD
        config["_INPUT_DAY_COMPACT"] = crawl_day.replace("-", "")
    else:
        # 多日：不强制全局前缀，_result_folder_name 用各 date_str
        config.pop("_INPUT_DAY_COMPACT", None)
        config.pop("_CRAWL_DAY", None)

    if progress_enabled(config):
        if multi_day:
            for d in ok_dates:
                day_items = [
                    ev
                    for ev in raw_events
                    if isinstance(ev, dict)
                    and (ev.get("event_date") or "").strip() == d
                ]
                if not day_items:
                    continue
                sync_items(config, d, day_items)
                report_day(
                    config,
                    d,
                    status="running",
                    message="event_sync 本机爬取中（多日）",
                    titles=grouped.get(d) or [],
                )
        elif not crawl_day:
            logger.warning(
                "已开启 OPINION_PROGRESS_ENABLED，但无法确定采集日"
                "（请用 --start-date=--end-date 或配置 OPINION_CRAWL_DAY），跳过进度回写"
            )
        else:
            # 用本机已拉事件推送落库（避免 .10 sync-day 回退 mock）
            day_items = [
                ev
                for ev in raw_events
                if isinstance(ev, dict)
                and (ev.get("event_date") or "").strip() in (crawl_day, *ok_dates)
            ]
            if not day_items:
                day_items = [ev for ev in raw_events if isinstance(ev, dict)]
            sync_items(config, crawl_day, day_items)
            all_titles: list[str] = []
            for d in ok_dates:
                all_titles.extend(grouped.get(d) or [])
            report_day(
                config,
                crawl_day,
                status="running",
                message="event_sync 本机爬取中",
                titles=all_titles,
            )

    # 全部日期写完后再启爬虫（http：按事件落盘 年/月/日/YYYYMMDD-事件id）
    crawled = launch_crawlers(
        config,
        ok_dates,
        keywords_by_date={d: grouped[d] for d in ok_dates if d in grouped},
        events_by_date={d: events_by_date[d] for d in ok_dates if d in events_by_date},
    )

    if progress_enabled(config):
        # 单条进度已在 crawler 内按结果目录回写；此处仅把仍 running 的标失败
        days_to_close = list(ok_dates) if multi_day else ([crawl_day] if crawl_day else [])
        for d in days_to_close:
            report_day(
                config,
                d,
                status="failed",
                message="event_sync 结束时仍未完成",
                only_statuses=["running"],
            )

    logger.info("流程结束；爬虫成功日期: %s", crawled)
    logger.info("-------------------------------------------------------------------------")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())