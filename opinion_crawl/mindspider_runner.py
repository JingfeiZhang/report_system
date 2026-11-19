"""调用 MindSpider 关键词爬取 CLI（子进程，避免污染 FastAPI 进程）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

_SUPPORTED = ("xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu")


def mindspider_root() -> Path:
    raw = (os.getenv("OPINION_CRAWL_MINDSPIDER_ROOT") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / "MindSpider"


def cli_script() -> Path:
    return mindspider_root() / "crawl_keywords_cli.py"


def configured_platforms() -> List[str]:
    raw = (os.getenv("OPINION_CRAWL_PLATFORMS") or "wb").strip()
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    out = [p for p in parts if p in _SUPPORTED]
    return out or ["wb"]


def login_type() -> str:
    raw = (os.getenv("OPINION_CRAWL_LOGIN_TYPE") or "cookie").strip().lower()
    return raw if raw in {"qrcode", "phone", "cookie"} else "cookie"


def max_notes() -> int:
    try:
        return max(1, int(os.getenv("OPINION_CRAWL_MAX_NOTES") or "20"))
    except ValueError:
        return 20


def crawl_timeout_sec() -> int:
    try:
        return max(60, int(os.getenv("OPINION_CRAWL_TIMEOUT_SEC") or "3600"))
    except ValueError:
        return 3600


def dry_run_enabled() -> bool:
    raw = (os.getenv("OPINION_CRAWL_MINDSPIDER_DRY_RUN") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def crawl_keywords(
    keywords: Sequence[str],
    *,
    platforms: Optional[Sequence[str]] = None,
    login: Optional[str] = None,
    notes: Optional[int] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """同步执行一次关键词爬取，返回结果 dict。"""
    kws = [str(k).strip() for k in keywords if str(k).strip()]
    if not kws:
        return {"success": False, "error": "keywords empty"}

    plats = list(platforms) if platforms else configured_platforms()
    plats = [p for p in plats if p in _SUPPORTED] or ["wb"]
    lt = login or login_type()
    mn = notes if notes is not None else max_notes()
    is_dry = dry_run_enabled() if dry_run is None else bool(dry_run)

    script = cli_script()
    if not script.is_file():
        return {"success": False, "error": f"MindSpider CLI 不存在: {script}"}

    cmd = [
        sys.executable,
        str(script),
        "--keywords",
        *kws,
        "--platforms",
        *plats,
        "--max-notes",
        str(mn),
        "--lt",
        lt,
    ]
    if is_dry:
        cmd.append("--dry-run")

    logger.info(
        "MindSpider crawl start keywords={} platforms={} dry_run={}",
        kws[:5],
        plats,
        is_dry,
    )
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(mindspider_root()),
            capture_output=True,
            text=True,
            timeout=crawl_timeout_sec(),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"MindSpider 超时（>{crawl_timeout_sec()}s）"}
    except Exception as exc:
        logger.exception("MindSpider subprocess failed: {}", exc)
        return {"success": False, "error": str(exc)}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    parsed: Dict[str, Any] = {}
    # CLI 最后一行应为 JSON
    for line in reversed(stdout.splitlines() if stdout else []):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if not parsed:
        parsed = {
            "success": completed.returncode == 0,
            "stdout_tail": stdout[-800:] if stdout else "",
            "stderr_tail": stderr[-800:] if stderr else "",
        }

    if completed.returncode != 0 and "success" not in parsed:
        parsed["success"] = False
    if completed.returncode != 0:
        parsed.setdefault("error", f"exit={completed.returncode}")
        if stderr:
            parsed.setdefault("stderr_tail", stderr[-500:])

    parsed["returncode"] = completed.returncode
    parsed["keywords"] = kws
    parsed["platforms"] = plats
    logger.info(
        "MindSpider crawl done success={} returncode={}",
        parsed.get("success"),
        completed.returncode,
    )
    return parsed
