#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写库完成后启动爬虫：CLI（源码 MindSpider）或 HTTP（mindspider-api）。"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("event_sync.crawler")

_WORKER_KILL_LOCK = threading.Lock()

try:
    from api_client import event_biz_id
except ImportError:  # pragma: no cover
    def event_biz_id(ev: Dict[str, Any]) -> Optional[int]:
        for key in ("id", "main_event_id", "record_event_id"):
            val = ev.get(key)
            if val is None or val == "":
                continue
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
        return None


def _truthy(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def _should_skip_title(title: str) -> bool:
    """明显不可爬的标题直接跳过，避免白耗 worker。"""
    t = (title or "").strip()
    if not t:
        return True
    low = t.lower()
    if "403" in low and ("禁止" in t or "forbidden" in low or "禁止访问" in t):
        return True
    if t.count("错误403") >= 1 or t.count("禁止访问") >= 2:
        return True
    return False


def _kill_platform_worker(config: Dict[str, str], *, reason: str) -> None:
    """假死时杀掉容器内 MediaCrawler，让后续任务能继续。"""
    container = (config.get("CRAWLER_DOCKER_CONTAINER") or "mindspider-api").strip()
    if not container:
        return
    with _WORKER_KILL_LOCK:
        logger.warning("终止假死 worker container=%s reason=%s", container, reason)
        try:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "sh",
                    "-c",
                    "pkill -f 'python main.py --platform' || true",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except Exception:
            logger.exception("终止 worker 失败 container=%s", container)


def _skip_event_ids(config: Dict[str, str]) -> set[str]:
    """已完成勿重爬：CRAWLER_SKIP_EVENT_IDS 或 resume JSON 的 completed_ok_ids。"""
    import json

    ids: set[str] = set()
    raw = (config.get("CRAWLER_SKIP_EVENT_IDS") or "").strip()
    if raw:
        for part in raw.replace(";", ",").split(","):
            p = part.strip()
            if p:
                ids.add(p)
    resume = (config.get("CRAWLER_RESUME_FILE") or "").strip()
    if resume:
        path = Path(resume)
        if not path.is_file():
            path = Path(__file__).resolve().parent / resume
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                for x in data.get("completed_ok_ids") or []:
                    ids.add(str(x).strip())
            except Exception:
                logger.exception("读取续跑文件失败: %s", path)
        else:
            logger.warning("续跑文件不存在: %s", path)
    return ids


def _infer_event_type(ev: Dict[str, Any], eid: Optional[int]) -> str:
    et = str(ev.get("event_type") or "").strip().lower()
    if et in ("monitor", "record"):
        return et
    if ev.get("record_event_id") is not None and ev.get("main_event_id") is None:
        return "record"
    if eid is not None and eid < 100000:
        return "record"
    return "monitor"


def _report_event_progress(
    config: Dict[str, str],
    ev: Dict[str, Any],
    eid: Optional[int],
    *,
    status: str,
    message: str = "",
) -> None:
    if eid is None:
        return
    try:
        from progress_reporter import report_event
    except ImportError:
        return
    report_event(
        config,
        event_type=_infer_event_type(ev, eid),
        event_id=int(eid),
        status=status,
        message=message,
    )


def _run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Windows 下避免默认 GBK 解码容器/子进程输出失败。"""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)


def _platforms(config: Dict[str, str]) -> List[str]:
    return [
        p.strip()
        for p in (config.get("CRAWLER_PLATFORMS") or "").split(",")
        if p.strip()
    ]


def _limit_keywords(keywords: List[str], config: Dict[str, str]) -> List[str]:
    raw = (config.get("CRAWLER_MAX_KEYWORDS") or "").strip()
    if not raw:
        return list(keywords)
    try:
        n = int(raw)
    except ValueError:
        return list(keywords)
    if n <= 0:
        return list(keywords)
    return list(keywords[:n])


def _cookie_specs() -> Dict[str, Dict[str, str]]:
    """平台 → Cookie 文件名与必含标记。"""
    return {
        "wb": {
            "file": "wb_cookies.txt",
            "marker": "SUB=",
            "config_key": "CRAWLER_COOKIES_FILE",
            "finished_hint": "Weibo Crawler finished",
            "update_hint": "update_weibo_note",
        },
        "zhihu": {
            "file": "zhihu_cookies.txt",
            "marker": "z_c0=",
            "config_key": "CRAWLER_ZHIHU_COOKIES_FILE",
            "finished_hint": "Zhihu Crawler finished",
            "update_hint": "zhihu",
        },
        "xhs": {
            "file": "xhs_cookies.txt",
            "marker": "web_session=",
            "config_key": "CRAWLER_XHS_COOKIES_FILE",
            "finished_hint": "XiaoHongShu Crawler finished",
            "update_hint": "xhs_note",
        },
    }


def _resolve_platform_cookies_file(
    config: Dict[str, str], platform: str
) -> Optional[Path]:
    spec = _cookie_specs().get(platform)
    if not spec:
        return None
    raw = (config.get(spec["config_key"]) or "").strip()
    if not raw and platform == "wb":
        raw = (config.get("CRAWLER_COOKIES_FILE") or "").strip()
    if not raw:
        # mindspider_api_local 已挪到 BettaFish_Modify 上一级
        project_root = Path(__file__).resolve().parent.parent
        raw = str(project_root.parent / "mindspider_api_local" / spec["file"])
    p = Path(raw)
    if not p.is_absolute():
        p = (Path(__file__).resolve().parent / p).resolve()
    return p if p.is_file() else None


def _resolve_cookies_file(config: Dict[str, str]) -> Optional[Path]:
    """兼容旧调用：默认微博 Cookie 文件。"""
    return _resolve_platform_cookies_file(config, "wb")


def _read_cookie_string(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and "=" in ln
    ]
    if not lines:
        return ""
    return max(lines, key=len)


def _inject_platform_cookie_login(config: Dict[str, str], platform: str) -> bool:
    """把宿主机 Cookie 注入 mindspider-api 并做一次 cookie 登录探测。"""
    spec = _cookie_specs().get(platform)
    if not spec:
        return False

    cookie_path = _resolve_platform_cookies_file(config, platform)
    if cookie_path is None:
        logger.warning(
            "未找到 %s Cookie 文件（默认 mindspider_api_local/%s），跳过注入",
            platform,
            spec["file"],
        )
        return False

    cookie = _read_cookie_string(cookie_path)
    marker = spec["marker"]
    if len(cookie) < 20 or marker not in cookie:
        logger.error(
            "%s Cookie 无效或不完整: %s（需含 %s）",
            platform,
            cookie_path,
            marker,
        )
        return False

    container = (config.get("CRAWLER_DOCKER_CONTAINER") or "mindspider-api").strip()
    project_root = Path(__file__).resolve().parent.parent
    local_dir = project_root.parent / "mindspider_api_local"
    if not local_dir.is_dir():
        local_dir = project_root / "mindspider_api_local"
    inner_sh = local_dir / "login_platform_cookie_inner.sh"
    if not inner_sh.is_file():
        logger.error("缺少注入脚本: %s", inner_sh)
        return False

    import tempfile

    script_body = (
        inner_sh.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    )
    remote_cookie = f"/tmp/{platform}_cookies.txt"
    remote_script = "/tmp/login_platform_cookie_inner.sh"

    logger.info(
        "向容器 %s 注入 %s Cookie（来自 %s，长度=%d）...",
        container,
        platform,
        cookie_path,
        len(cookie),
    )
    try:
        with tempfile.TemporaryDirectory() as td:
            cookie_tmp = Path(td) / f"{platform}_cookies.txt"
            script_tmp = Path(td) / "login_platform_cookie_inner.sh"
            cookie_tmp.write_bytes(
                cookie.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            )
            script_tmp.write_bytes(script_body.encode("utf-8"))
            try:
                _run(
                    ["docker", "cp", str(cookie_tmp), f"{container}:{remote_cookie}"],
                    check=True,
                )
                _run(
                    ["docker", "cp", str(script_tmp), f"{container}:{remote_script}"],
                    check=True,
                )
                completed = _run(
                    [
                        "docker",
                        "exec",
                        "-e",
                        f"PLATFORM={platform}",
                        "-e",
                        f"COOKIE_FILE={remote_cookie}",
                        "-e",
                        f"COOKIE_MARKER={marker}",
                        container,
                        "sh",
                        remote_script,
                    ],
                    check=False,
                )
            finally:
                _run(
                    [
                        "docker",
                        "exec",
                        container,
                        "rm",
                        "-f",
                        remote_cookie,
                        remote_script,
                    ],
                    check=False,
                )
    except FileNotFoundError:
        logger.error("未找到 docker 命令，无法自动注入 Cookie")
        return False
    except subprocess.CalledProcessError as e:
        logger.error("docker cp/exec 失败 platform=%s: %s", platform, e.stderr or e)
        return False

    if completed.stdout:
        logger.info("%s Cookie 登录 stdout:\n%s", platform, completed.stdout[-3000:])
    if completed.stderr:
        logger.warning("%s Cookie 登录 stderr:\n%s", platform, completed.stderr[-3000:])

    out = (completed.stdout or "") + (completed.stderr or "")
    login_ok = (
        completed.returncode == 0
        or spec["finished_hint"] in out
        or spec["update_hint"] in out
        or "Login successful" in out
        or "login success" in out.lower()
    )
    hard_fail = ("cookie may be invalid" in out.lower()) or (
        "login failed" in out.lower() and "qrcode" in out.lower()
    )
    table_missing = "doesn't exist" in out or "1146" in out

    if hard_fail and not login_ok:
        logger.error(
            "%s Cookie 无效或登录失败 returncode=%s；请更新 %s 后重试",
            platform,
            completed.returncode,
            spec["file"],
        )
        return False
    if table_missing and not login_ok:
        logger.warning(
            "%s Cookie 登录过程中数据库缺表（returncode=%s）；仍视为注入已执行",
            platform,
            completed.returncode,
        )
        return True
    if not login_ok:
        logger.error(
            "%s Cookie 注入/登录未成功 returncode=%s；请查看上方日志",
            platform,
            completed.returncode,
        )
        return False

    logger.info(
        "%s Cookie 注入完成，登录态: browser_data/%s_user_data_dir",
        platform,
        platform,
    )
    return True


def ensure_cookie_logins(config: Dict[str, str]) -> bool:
    """按 CRAWLER_PLATFORMS 注入 wb/zhihu/xhs Cookie（有文件才注）。"""
    if not _truthy(config.get("CRAWLER_ENSURE_COOKIE_LOGIN", "true")):
        logger.info("CRAWLER_ENSURE_COOKIE_LOGIN=false，跳过 Cookie 注入")
        return False

    platforms = _platforms(config)
    wanted = [p for p in ("wb", "zhihu", "xhs") if p in platforms]
    if not wanted:
        return False

    ok_any = False
    for p in wanted:
        if _inject_platform_cookie_login(config, p):
            ok_any = True
    return ok_any


def ensure_weibo_cookie_login(config: Dict[str, str]) -> bool:
    """兼容旧名。"""
    return ensure_cookie_logins(config)


def launch_crawlers(
    config: Dict[str, str],
    dates: List[str],
    keywords_by_date: Optional[Dict[str, List[str]]] = None,
    events_by_date: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[str]:
    """
    按日期顺序启动爬虫。返回成功日期列表。

    CRAWLER_BACKEND:
      - cli（默认）：子进程调用 MindSpider/main.py --deep-sentiment（读 daily_topics）
      - http：调用 mindspider-api POST /crawl/tasks
        · 有 events_by_date 时按事件爬取，结果目录为
          {年}/{月}/{日}/{输入日YYYYMMDD}-{事件id}
        · 否则退回按日合并关键词（目录仍为 API task_id）
    """
    if not _truthy(config.get("CRAWLER_ENABLED")):
        logger.info("CRAWLER_ENABLED=false，跳过启动爬虫")
        return []

    if not dates:
        logger.info("无日期可爬，跳过")
        return []

    backend = (config.get("CRAWLER_BACKEND") or "cli").strip().lower()
    platforms = _platforms(config)
    if not platforms:
        logger.error("CRAWLER_PLATFORMS 为空，无法启动爬虫")
        return []

    logger.info("爬虫后端=%s 日期数=%d", backend, len(dates))
    if backend in ("http", "api", "mindspider-api"):
        ensure_cookie_logins(config)
        return _launch_http(
            config,
            dates,
            platforms,
            keywords_by_date or {},
            events_by_date or {},
        )
    if backend in ("cli", "local", "subprocess"):
        return _launch_cli(config, dates, platforms)
    logger.error("未知 CRAWLER_BACKEND=%s（支持 cli / http）", backend)
    return []


def _result_folder_name(config: Dict[str, str], date_str: str, event: Dict[str, Any]) -> str:
    """分级结果相对路径：{年}/{月}/{日}/{输入日YYYYMMDD}-{事件id}。

    例：2026/08/09/20260809-396733
    """
    compact = (config.get("_INPUT_DAY_COMPACT") or "").strip()
    if not compact:
        compact = date_str.replace("-", "")
    if len(compact) >= 8 and compact[:8].isdigit():
        yyyy, mm, dd = compact[:4], compact[4:6], compact[6:8]
    else:
        parts = (date_str or "").strip().split("-")
        if len(parts) >= 3:
            yyyy, mm, dd = parts[0], parts[1], parts[2]
        else:
            yyyy, mm, dd = "unknown", "00", "00"
    eid = event_biz_id(event)
    if eid is None:
        title = (event.get("title") or "unknown").strip()[:40]
        safe = "".join(c if c.isalnum() else "_" for c in title) or "unknown"
        leaf = f"{compact}-{safe}"
    else:
        leaf = f"{compact}-{eid}"
    return f"{yyyy}/{mm}/{dd}/{leaf}"


def _launch_cli(
    config: Dict[str, str], dates: List[str], platforms: List[str]
) -> List[str]:
    """
    注意：MindSpider 读取项目根目录 .env 的 DB_NAME，
    请确保与 event_sync/config.txt 中的 DB_NAME 一致。
    """
    project_root = Path(__file__).resolve().parent.parent
    mindspider_dir = project_root / "MindSpider"
    mindspider_main = mindspider_dir / "main.py"
    if not mindspider_main.is_file():
        raise FileNotFoundError(f"找不到 MindSpider 入口: {mindspider_main}")

    max_kw = config.get("CRAWLER_MAX_KEYWORDS", "10")
    max_notes = config.get("CRAWLER_MAX_NOTES", "10")
    env = os.environ.copy()

    ok_dates: List[str] = []
    for date_str in dates:
        cmd = [
            sys.executable,
            str(mindspider_main),
            "--deep-sentiment",
            "--date",
            date_str,
            "--platforms",
            *platforms,
            "--max-keywords",
            max_kw,
            "--max-notes",
            max_notes,
        ]
        logger.info("启动 CLI 爬虫: %s", " ".join(cmd))
        try:
            completed = _run(
                cmd,
                cwd=str(mindspider_dir),
                env=env,
                check=False,
            )
        except Exception:
            logger.exception("爬虫进程启动失败 date=%s", date_str)
            continue

        if completed.stdout:
            logger.info("爬虫 stdout [%s]:\n%s", date_str, completed.stdout[-4000:])
        if completed.stderr:
            logger.warning(
                "爬虫 stderr [%s]:\n%s", date_str, completed.stderr[-4000:]
            )

        if completed.returncode == 0:
            logger.info("CLI 爬虫完成 date=%s", date_str)
            ok_dates.append(date_str)
        else:
            logger.error(
                "CLI 爬虫失败 date=%s returncode=%s", date_str, completed.returncode
            )

    logger.info("CLI 爬虫成功日期: %s", ok_dates)
    return ok_dates


def _launch_http(
    config: Dict[str, str],
    dates: List[str],
    platforms: List[str],
    keywords_by_date: Dict[str, List[str]],
    events_by_date: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[str]:
    base = (
        config.get("MINDSPIDER_API_BASE")
        or config.get("CRAWLER_API_BASE")
        or ""
    ).rstrip("/")
    if not base:
        logger.error("HTTP 后端需要配置 MINDSPIDER_API_BASE（如 http://127.0.0.1:8082/api/v1）")
        return []

    timeout = int(config.get("MINDSPIDER_API_TIMEOUT") or config.get("API_TIMEOUT") or "30")
    poll_interval = int(config.get("CRAWLER_POLL_INTERVAL") or "5")
    poll_timeout = int(config.get("CRAWLER_POLL_TIMEOUT") or "7200")
    hard_timeout = int(config.get("CRAWLER_TASK_HARD_TIMEOUT") or "300")
    concurrency = max(1, int(config.get("CRAWLER_CONCURRENCY") or "2"))
    kill_on_timeout = _truthy(config.get("CRAWLER_KILL_ON_TIMEOUT", "true"))
    max_notes = int(config.get("CRAWLER_MAX_NOTES") or "20")
    login_type = (config.get("CRAWLER_LOGIN_TYPE") or "qrcode").strip()
    pack_format = (config.get("CRAWLER_PACK_FORMAT") or "dir").strip()
    do_download = _truthy(config.get("CRAWLER_DOWNLOAD", "true"))
    keep_archive = _truthy(config.get("CRAWLER_KEEP_ARCHIVE", "false"))
    _project_root = Path(__file__).resolve().parent.parent
    _parent = _project_root.parent
    local_exports = Path(
        config.get("CRAWLER_LOCAL_EXPORTS_DIR")
        or (_parent / "mindspider_api_local" / "exports")
    )

    download_dir = Path(
        config.get("CRAWLER_DOWNLOAD_DIR")
        or (_parent / "exports" / "event_sync")
    )
    if do_download:
        download_dir.mkdir(parents=True, exist_ok=True)

    events_by_date = events_by_date or {}
    ok_dates: List[str] = []
    for date_str in dates:
        day_events = list(events_by_date.get(date_str) or [])
        if day_events:
            # 按事件异步爬：结果目录 = {年}/{月}/{日}/{输入日YYYYMMDD}-{事件id}
            limited = day_events
            max_kw = int(config.get("CRAWLER_MAX_KEYWORDS") or "0")
            if max_kw > 0:
                limited = day_events[:max_kw]
            skip_ids = _skip_event_ids(config)
            if skip_ids:
                logger.info(
                    "续跑跳过已完成事件 %d 个: %s",
                    len(skip_ids),
                    ",".join(sorted(skip_ids, key=lambda x: int(x) if x.isdigit() else 0)),
                )

            work: List[Tuple[Dict[str, Any], Optional[int], str, str]] = []
            for ev in limited:
                title = (ev.get("title") or "").strip()
                if not title:
                    continue
                if _should_skip_title(title):
                    eid = event_biz_id(ev)
                    logger.warning(
                        "跳过不可爬标题 date=%s event_id=%s title=%s",
                        date_str,
                        eid,
                        title[:60],
                    )
                    _report_event_progress(
                        config,
                        ev,
                        eid,
                        status="failed",
                        message="标题疑似不可爬（403/禁止访问），已跳过",
                    )
                    continue
                folder = _result_folder_name(config, date_str, ev)
                eid = event_biz_id(ev)
                if eid is not None and str(eid) in skip_ids:
                    logger.info(
                        "跳过已完成事件 date=%s event_id=%s folder=%s",
                        date_str,
                        eid,
                        folder,
                    )
                    continue
                work.append((ev, eid, folder, title))

            event_max_wait = hard_timeout if hard_timeout > 0 else poll_timeout
            logger.info(
                "按事件异步爬取 date=%s events=%d concurrency=%d hard_timeout=%ss",
                date_str,
                len(work),
                concurrency,
                event_max_wait,
            )

            def _crawl_one(
                item: Tuple[Dict[str, Any], Optional[int], str, str],
            ) -> bool:
                ev, eid, folder, title = item
                logger.info(
                    "按事件爬取 date=%s event_id=%s folder=%s title=%s",
                    date_str,
                    eid,
                    folder,
                    title[:60],
                )
                ok = _run_one_http_task(
                    base=base,
                    keywords=[title],
                    platforms=platforms,
                    max_notes=max_notes,
                    login_type=login_type,
                    pack_format=pack_format,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    poll_timeout=event_max_wait,
                    do_download=do_download,
                    download_dir=download_dir,
                    local_exports=local_exports,
                    keep_archive=keep_archive,
                    save_as=folder,
                    log_key=f"{date_str}/{eid}",
                    on_timeout=(
                        (
                            lambda tid, _eid=eid: _kill_platform_worker(
                                config,
                                reason=f"hard_timeout task_id={tid} event_id={_eid}",
                            )
                        )
                        if kill_on_timeout
                        else None
                    ),
                )
                folder_ok = bool(ok and folder and (download_dir / folder).is_dir())
                _report_event_progress(
                    config,
                    ev,
                    eid,
                    status="completed" if folder_ok else "failed",
                    message=(
                        f"结果目录已落盘 {folder}"
                        if folder_ok
                        else f"爬取失败/超时或未落盘 folder={folder}"
                    ),
                )
                return folder_ok

            day_ok = False
            if work:
                with ThreadPoolExecutor(
                    max_workers=concurrency, thread_name_prefix="crawl-ev"
                ) as pool:
                    futures = [pool.submit(_crawl_one, item) for item in work]
                    for fut in as_completed(futures):
                        try:
                            if fut.result():
                                day_ok = True
                        except Exception:
                            logger.exception("异步爬取任务异常 date=%s", date_str)
            if day_ok:
                ok_dates.append(date_str)
            continue

        keywords = _limit_keywords(keywords_by_date.get(date_str) or [], config)
        if not keywords:
            logger.error(
                "HTTP 爬虫跳过 date=%s：无关键词（请传入 events_by_date 或 keywords_by_date）",
                date_str,
            )
            continue

        logger.info(
            "按日合并爬取 date=%s keywords=%d（无事件 id，目录仍用 task_id）",
            date_str,
            len(keywords),
        )
        day_wait = min(poll_timeout, hard_timeout) if hard_timeout > 0 else poll_timeout
        ok = _run_one_http_task(
            base=base,
            keywords=keywords,
            platforms=platforms,
            max_notes=max_notes,
            login_type=login_type,
            pack_format=pack_format,
            timeout=timeout,
            poll_interval=poll_interval,
            poll_timeout=day_wait,
            do_download=do_download,
            download_dir=download_dir,
            local_exports=local_exports,
            keep_archive=keep_archive,
            save_as=None,
            log_key=date_str,
            on_timeout=(
                (
                    lambda tid: _kill_platform_worker(
                        config, reason=f"hard_timeout day-task task_id={tid}"
                    )
                )
                if kill_on_timeout
                else None
            ),
        )
        if ok:
            ok_dates.append(date_str)

    logger.info("HTTP 爬虫成功日期: %s", ok_dates)
    return ok_dates


def _run_one_http_task(
    *,
    base: str,
    keywords: List[str],
    platforms: List[str],
    max_notes: int,
    login_type: str,
    pack_format: str,
    timeout: int,
    poll_interval: int,
    poll_timeout: int,
    do_download: bool,
    download_dir: Path,
    local_exports: Path,
    keep_archive: bool,
    save_as: Optional[str],
    log_key: str,
    on_timeout=None,
) -> bool:
    payload = {
        "keywords": keywords,
        "platforms": platforms,
        "max_notes": max_notes,
        "login_type": login_type,
        "pack_format": pack_format,
    }
    create_url = f"{base}/crawl/tasks"
    logger.info(
        "创建 HTTP 爬取任务 key=%s keywords=%d url=%s", log_key, len(keywords), create_url
    )
    try:
        resp = requests.post(create_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        logger.exception("创建任务失败 key=%s", log_key)
        return False

    task_id = body.get("task_id")
    if not task_id:
        logger.error("创建任务无 task_id key=%s body=%s", log_key, body)
        return False

    logger.info(
        "任务已创建 key=%s task_id=%s status=%s", log_key, task_id, body.get("status")
    )
    final = _poll_task(
        base,
        task_id,
        timeout=timeout,
        interval=poll_interval,
        max_wait=poll_timeout,
    )
    if not final:
        logger.error("轮询超时或失败 key=%s task_id=%s", log_key, task_id)
        if on_timeout:
            try:
                on_timeout(task_id)
            except Exception:
                logger.exception("超时回调失败 key=%s task_id=%s", log_key, task_id)
        return False

    status = (final.get("status") or "").lower()
    if status not in ("success", "failed"):
        logger.error(
            "任务超过硬超时仍未结束 key=%s task_id=%s status=%s",
            log_key,
            task_id,
            status,
        )
        if on_timeout:
            try:
                on_timeout(task_id)
            except Exception:
                logger.exception("超时回调失败 key=%s task_id=%s", log_key, task_id)
        return False

    if status != "success":
        logger.error(
            "任务失败 key=%s task_id=%s status=%s message=%s",
            log_key,
            task_id,
            status,
            final.get("message") or final.get("error"),
        )
        return False

    logger.info(
        "HTTP 爬虫成功 key=%s task_id=%s save_as=%s stats=%s",
        log_key,
        task_id,
        save_as or task_id,
        final.get("stats"),
    )
    if do_download:
        try:
            _save_results(
                base,
                task_id,
                download_dir,
                pack_format,
                timeout=timeout,
                local_exports=local_exports,
                keep_archive=keep_archive,
                save_as=save_as,
            )
        except Exception:
            logger.exception(
                "保存结果失败 key=%s task_id=%s（任务本身已成功）", log_key, task_id
            )
    return True


def _poll_task(
    base: str,
    task_id: str,
    *,
    timeout: int,
    interval: int,
    max_wait: int,
) -> Optional[dict]:
    url = f"{base}/crawl/tasks/{task_id}"
    deadline = time.time() + max_wait
    last: Optional[dict] = None
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            last = resp.json()
        except Exception:
            logger.exception("轮询任务异常 task_id=%s", task_id)
            time.sleep(interval)
            continue

        status = (last.get("status") or "").lower()
        logger.info("轮询 task_id=%s status=%s", task_id, status)
        if status in ("success", "failed"):
            return last
        time.sleep(interval)
    return last


def _extract_archive(archive: Path, dest_dir: Path) -> Path:
    """解压 zip/tar.gz 到 dest_dir/{task_id 或顶层目录}，返回结果目录。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(dest_dir)
    else:
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest_dir)

    # 常见结构：dest_dir/task_xxx/*.csv
    candidates = [p for p in dest_dir.iterdir() if p.is_dir()]
    if len(candidates) == 1:
        return candidates[0]
    # 扁平解压到 dest_dir 本身
    return dest_dir


def _copy_local_export(
    local_exports: Path,
    task_id: str,
    download_dir: Path,
    *,
    save_as: Optional[str] = None,
) -> Optional[Path]:
    src = local_exports / task_id
    if not src.is_dir():
        return None
    has_files = any(p.is_file() for p in src.rglob("*"))
    if not has_files:
        return None
    dest_name = save_as or task_id
    dest = download_dir / Path(dest_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    logger.info("已从本机 API exports 复制目录 %s -> %s", src, dest)
    return dest


def _save_results(
    base: str,
    task_id: str,
    download_dir: Path,
    pack_format: str,
    *,
    timeout: int,
    local_exports: Path,
    keep_archive: bool,
    save_as: Optional[str] = None,
) -> Path:
    """保存爬取结果为可直接查看的文件夹。

    save_as：相对路径（如 2026/08/09/20260809-396733）；默认仍用 API task_id。
    """
    dest_name = save_as or task_id
    dest_rel = Path(dest_name)
    leaf = dest_rel.name

    # 1) 同机：mindspider_api_local/exports/{task_id} 已有 CSV
    copied = _copy_local_export(
        local_exports, task_id, download_dir, save_as=dest_name
    )
    if copied is not None:
        return copied

    # 2) HTTP 下载（dir 格式时 API 下载接口会临时打 zip，不落盘持久化）
    ext = "tar.gz" if pack_format.strip().lower() == "tar.gz" else "zip"
    # 临时包与解压目录用叶子名，避免路径分隔符干扰
    archive = download_dir / f"{leaf}.{ext}"
    url = f"{base}/crawl/tasks/{task_id}/download"
    logger.info("下载结果包 %s -> %s", url, archive)
    with requests.get(url, stream=True, timeout=max(timeout, 120)) as resp:
        resp.raise_for_status()
        with open(archive, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    logger.info("已下载 %s (%d bytes)", archive, archive.stat().st_size)

    # 统一解压成文件夹，便于直接查看 CSV
    out_dir = download_dir / dest_rel
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    tmp_root = download_dir / f".extract_{leaf}"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    extracted = _extract_archive(archive, tmp_root)
    if extracted.resolve() == tmp_root.resolve():
        shutil.move(str(tmp_root), str(out_dir))
    else:
        shutil.move(str(extracted), str(out_dir))
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
    logger.info("已解压为目录 %s", out_dir)
    if not keep_archive and archive.exists():
        archive.unlink()
        logger.info("已删除压缩包 %s（CRAWLER_KEEP_ARCHIVE=false）", archive)
    return out_dir
