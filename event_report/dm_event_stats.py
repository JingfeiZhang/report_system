# -*- coding: utf-8 -*-
"""EventReport 达梦聚合：按事件标题 / topic_id / 日期窗口统计热度、帖子与词云。

关联键（表无 main_event_id）：
  source_keyword ≈ title  或  topic_id = summary_YYYYMMDD

演示模式（EVENT_REPORT_DEMO_MODE）：
  仅查 EVENT_REPORT_DEMO_COMMENT_TABLE（默认 weibo_note_comment_copy1），
  不按机密事件标题过滤，便于左屏 sensitive / impact 演示。
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

QueryFn = Callable[[str, Optional[Dict[str, Any]]], List[Dict[str, Any]]]

_STOPWORDS = frozenset(
    list("的了是在不有和与及对为也等")
    + "这个 那个 他们 我们 你们 什么 怎么 如何 如果 因为 所以 以及 进行 通过 可以 "
    "一个 一些 没有 已经 还是 就是 不是 只是 都是 而且 但是 然后 关于".split()
    + list("一二三四五六七八九十百千万")
)

_PLATFORM_LABEL = {
    "weibo_note": "微博",
    "xhs_note": "小红书",
    "zhihu_content": "知乎",
    "weibo_note_comment": "微博",
    "weibo_note_comment_copy1": "微博",
}


@dataclass
class PostRow:
    platform: str
    account_name: str
    content: str
    heat: int
    published_at: Optional[str] = None
    url: Optional[str] = None
    source_keyword: Optional[str] = None
    source_table: str = ""


@dataclass
class DailyHeatResult:
    """按日热度；heat_by_date 的 key 为 YYYY-MM-DD。"""

    heat_by_date: Dict[str, int] = field(default_factory=dict)
    total_posts: int = 0
    error: Optional[str] = None


def dm_enabled() -> bool:
    raw = (os.getenv("EVENT_REPORT_DM_ENABLED") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def demo_mode_enabled() -> bool:
    """演示模式：优先读演示评论表；无数据再灌合成样例（对齐 EVENT2_DEMO_MODE）。"""
    raw = (os.getenv("EVENT_REPORT_DEMO_MODE") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def demo_comment_table() -> str:
    """演示专用评论表名（默认 weibo_note_comment_copy1）。"""
    name = (os.getenv("EVENT_REPORT_DEMO_COMMENT_TABLE") or "weibo_note_comment_copy1").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return "weibo_note_comment_copy1"
    return name


def _to_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        s = str(val).strip().replace(",", "")
        if not s or s.lower() in ("none", "null", "-"):
            return 0
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _is_comment_table(table: str) -> bool:
    t = (table or "").lower()
    return "comment" in t


def _post_heat(row: Dict[str, Any], table: str) -> int:
    """单帖热度：liked*1 + comments*3 + shared*2（计划约定）。"""
    if _is_comment_table(table):
        likes = _to_int(row.get("comment_like_count") or row.get("liked_count"))
        subs = _to_int(row.get("sub_comment_count"))
        return max(1, likes * 1 + subs * 2 + 40)
    if table == "weibo_note":
        return (
            _to_int(row.get("liked_count")) * 1
            + _to_int(row.get("comments_count")) * 3
            + _to_int(row.get("shared_count")) * 2
        )
    if table == "xhs_note":
        return (
            _to_int(row.get("liked_count")) * 1
            + _to_int(row.get("comment_count")) * 3
            + _to_int(row.get("collected_count")) * 2
        )
    if table == "zhihu_content":
        return (
            _to_int(row.get("voteup_count") or row.get("liked_count")) * 1
            + _to_int(row.get("comment_count")) * 3
        )
    return max(
        1,
        _to_int(row.get("liked_count"))
        + _to_int(row.get("comments_count") or row.get("comment_count")),
    )


def _row_date(row: Dict[str, Any]) -> Optional[str]:
    for key in (
        "create_date_time",
        "publish_time",
        "time",
        "create_time",
        "last_modify_ts",
        "add_ts",
    ):
        raw = row.get(key)
        if raw is None:
            continue
        if isinstance(raw, datetime):
            return raw.strftime("%Y-%m-%d")
        s = str(raw).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        # 毫秒/秒时间戳
        if s.isdigit() or (s.replace(".", "", 1).isdigit()):
            try:
                val = float(s)
                # 过小的伪时间戳忽略（如演示表 add_ts=54220000）
                if val < 1_000_000_000:
                    continue
                ts = val / 1000 if val > 1_000_000_000_000 else val
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError):
                continue
    return None


def _window_dates(event_date: str, before: int, after: int) -> tuple[str, str, List[str]]:
    ed = datetime.strptime(event_date, "%Y-%m-%d")
    start = ed - timedelta(days=before)
    end = ed + timedelta(days=after)
    days: List[str] = []
    cur = start
    while cur <= end:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), days


def _topic_id_for_date(event_date: str) -> str:
    compact = event_date.replace("-", "")
    return f"summary_{compact}"


def _default_query_fn() -> Optional[QueryFn]:
    try:
        from InsightEngine.tools.search import MediaCrawlerDB

        db = MediaCrawlerDB()
        return db._execute_query  # noqa: SLF001 — 复用现有达梦连接
    except Exception as e:
        logger.warning("EventReport 无法初始化达梦客户端: {!r}", e)
        return None


def _match_clause(title_col: str = "source_keyword") -> str:
    # topic_id 可能不存在于所有环境；查询层用 try/except 降级
    return (
        f'("{title_col}" = :title OR "{title_col}" LIKE :title_like '
        f'OR "topic_id" = :topic_id)'
    )


def _fetch_table_rows(
    query_fn: QueryFn,
    table: str,
    *,
    title: str,
    topic_id: str,
    start: str,
    end: str,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    title = (title or "").strip()
    if not title and not topic_id:
        return []

    # 文本字段因表而异
    if table == "weibo_note":
        cols = (
            'nickname, content, liked_count, comments_count, shared_count, '
            "create_date_time, note_url, source_keyword, topic_id"
        )
        date_expr = 'SUBSTR("create_date_time", 1, 10)'
    elif table == "xhs_note":
        cols = (
            'nickname, title, desc, liked_count, comment_count, collected_count, '
            "time, note_url, source_keyword, topic_id"
        )
        date_expr = 'SUBSTR("time", 1, 10)'
    elif table == "zhihu_content":
        cols = (
            'user_nickname, title, desc, content_text, voteup_count, comment_count, '
            "created_time, content_url, source_keyword, topic_id"
        )
        date_expr = 'SUBSTR("created_time", 1, 10)'
    else:
        return []

    params = {
        "title": title[:200],
        "title_like": f"%{(title[:80] if title else '___')}%",
        "topic_id": topic_id,
        "start": start,
        "end": end,
        "lim": int(limit),
    }

    sql_full = (
        f"SELECT TOP :lim {cols} FROM \"{table}\" WHERE {_match_clause()} "
        f"AND {date_expr} BETWEEN :start AND :end"
    )
    try:
        rows = query_fn(sql_full, params)
        if rows:
            return rows
    except Exception as e:
        logger.debug("EventReport 查询 {} 带 topic_id 失败: {!r}", table, e)

    # 无 topic_id 列时降级
    sql_kw = (
        f'SELECT TOP :lim {cols.replace(", topic_id", "")} FROM "{table}" WHERE '
        f'("source_keyword" = :title OR "source_keyword" LIKE :title_like) '
        f"AND {date_expr} BETWEEN :start AND :end"
    )
    try:
        return query_fn(sql_kw, params) or []
    except Exception as e:
        logger.warning("EventReport 查询 {} 失败: {!r}", table, e)
        return []


def _normalize_post(row: Dict[str, Any], table: str) -> PostRow:
    platform = _PLATFORM_LABEL.get(table, "微博" if _is_comment_table(table) else table)
    if _is_comment_table(table):
        content = str(row.get("content") or "").strip()
        account = str(row.get("nickname") or "未知账号").strip() or "未知账号"
        url = row.get("profile_url") or row.get("note_url")
    elif table == "weibo_note":
        content = str(row.get("content") or "").strip()
        account = str(row.get("nickname") or "未知账号").strip() or "未知账号"
        url = row.get("note_url")
    elif table == "xhs_note":
        title = str(row.get("title") or "").strip()
        desc = str(row.get("desc") or "").strip()
        content = (title + " " + desc).strip() or title
        account = str(row.get("nickname") or "未知账号").strip() or "未知账号"
        url = row.get("note_url")
    else:
        title = str(row.get("title") or "").strip()
        body = str(row.get("content_text") or row.get("desc") or "").strip()
        content = (title + " " + body).strip() or title
        account = str(row.get("user_nickname") or row.get("nickname") or "未知账号").strip()
        url = row.get("content_url") or row.get("note_url")

    pub = _row_date(row)
    published_at = f"{pub}T12:00:00+08:00" if pub else None
    return PostRow(
        platform=platform,
        account_name=account[:64],
        content=content[:500],
        heat=max(1, _post_heat(row, table)),
        published_at=published_at,
        url=str(url) if url else None,
        source_keyword=str(row.get("source_keyword") or "") or None,
        source_table=table,
    )


def _fetch_demo_comment_rows(
    query_fn: QueryFn,
    *,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """演示表：不按 title 过滤，直接取 TOP N（机密事件标题对不上库）。"""
    table = demo_comment_table()
    params = {"lim": int(limit)}
    col_sets = [
        (
            "nickname, content, profile_url, add_ts, last_modify_ts, "
            "create_date_time, comment_like_count, sub_comment_count, note_id"
        ),
        "nickname, content, profile_url, add_ts, last_modify_ts, note_id",
        "nickname, content, last_modify_ts",
        "nickname, content",
    ]
    last_err: Optional[Exception] = None
    for cols in col_sets:
        sql = f'SELECT TOP :lim {cols} FROM "{table}"'
        try:
            rows = query_fn(sql, params) or []
            if rows:
                return rows
            # 空表也算成功
            return []
        except Exception as e:
            last_err = e
            logger.debug("EventReport 演示表 {} 列集失败 cols={!r} err={!r}", table, cols, e)
    logger.warning("EventReport 演示表 {} 查询失败: {!r}", table, last_err)
    return []


def _remap_rows_to_event_window(
    pairs: List[tuple[Dict[str, Any], str]],
    event_date: str,
    *,
    before: int,
    after: int,
) -> List[tuple[Dict[str, Any], str]]:
    """评论日期若不在事件窗内，按序摊到窗内各日，保证演示曲线非空。"""
    if not pairs:
        return pairs
    _, _, days = _window_dates(event_date, before, after)
    if not days:
        return pairs
    in_window = 0
    for row, _ in pairs:
        d = _row_date(row)
        if d and d in days:
            in_window += 1
    if in_window > 0:
        return pairs
    remapped: List[tuple[Dict[str, Any], str]] = []
    for i, (row, table) in enumerate(pairs):
        d = days[i % len(days)]
        new_row = dict(row)
        new_row["create_date_time"] = f"{d} 12:00:00"
        remapped.append((new_row, table))
    return remapped


def _collect_rows(
    title: str,
    event_date: str,
    *,
    before: int,
    after: int,
    limit_per_table: int,
    query_fn: Optional[QueryFn],
    tables: Sequence[str] = ("weibo_note", "xhs_note", "zhihu_content"),
) -> tuple[List[tuple[Dict[str, Any], str]], Optional[str]]:
    if query_fn is None:
        query_fn = _default_query_fn()
    if query_fn is None:
        return [], "dm_unavailable"

    # 演示模式：只查专用评论表，忽略普通 note 表与 title 匹配
    if demo_mode_enabled():
        table = demo_comment_table()
        rows = _fetch_demo_comment_rows(query_fn, limit=max(limit_per_table, 50))
        pairs = [(r, table) for r in rows]
        pairs = _remap_rows_to_event_window(
            pairs, event_date, before=before, after=after
        )
        return pairs, None

    start, end, _ = _window_dates(event_date, before, after)
    topic_id = _topic_id_for_date(event_date)
    out: List[tuple[Dict[str, Any], str]] = []
    for table in tables:
        rows = _fetch_table_rows(
            query_fn,
            table,
            title=title,
            topic_id=topic_id,
            start=start,
            end=end,
            limit=limit_per_table,
        )
        for r in rows:
            out.append((r, table))
    return out, None


def fetch_daily_heat(
    title: str,
    event_date: str,
    *,
    before: int = 5,
    after: int = 3,
    query_fn: Optional[QueryFn] = None,
) -> DailyHeatResult:
    """返回窗口内每日热度（互动加权分之和）。"""
    if not dm_enabled():
        return DailyHeatResult(error="dm_disabled")

    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        return DailyHeatResult(error="bad_event_date")

    _, _, days = _window_dates(event_date, before, after)
    heat_by_date = {d: 0 for d in days}

    pairs, err = _collect_rows(
        title,
        event_date,
        before=before,
        after=after,
        limit_per_table=800,
        query_fn=query_fn,
    )
    if err:
        return DailyHeatResult(heat_by_date=heat_by_date, error=err)

    total = 0
    for row, table in pairs:
        d = _row_date(row)
        if not d or d not in heat_by_date:
            # 无日期时记到事件日
            d = event_date if event_date in heat_by_date else None
        if not d:
            continue
        heat_by_date[d] = heat_by_date.get(d, 0) + _post_heat(row, table)
        total += 1

    return DailyHeatResult(heat_by_date=heat_by_date, total_posts=total)


def fetch_top_posts(
    title: str,
    event_date: str,
    *,
    before: int = 5,
    after: int = 3,
    limit: int = 10,
    query_fn: Optional[QueryFn] = None,
) -> List[PostRow]:
    if not dm_enabled():
        return []
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        return []

    pairs, err = _collect_rows(
        title,
        event_date,
        before=before,
        after=after,
        limit_per_table=400,
        query_fn=query_fn,
    )
    if err:
        return []

    posts = [_normalize_post(row, table) for row, table in pairs]
    posts.sort(key=lambda p: p.heat, reverse=True)
    # 去重：同账号+正文前 40 字
    seen = set()
    unique: List[PostRow] = []
    for p in posts:
        key = (p.account_name, p.content[:40])
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
        if len(unique) >= limit:
            break
    return unique


_CN_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,4}")


def fetch_word_cloud(
    title: str,
    event_date: str,
    *,
    before: int = 5,
    after: int = 3,
    limit: int = 20,
    query_fn: Optional[QueryFn] = None,
) -> List[Dict[str, Any]]:
    if not dm_enabled():
        return []
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        return []

    pairs, err = _collect_rows(
        title,
        event_date,
        before=before,
        after=after,
        limit_per_table=400,
        query_fn=query_fn,
    )
    if err or not pairs:
        return []

    counter: Counter[str] = Counter()
    for row, table in pairs:
        post = _normalize_post(row, table)
        text = post.content
        for tok in _CN_TOKEN.findall(text):
            if tok in _STOPWORDS or len(tok) < 2:
                continue
            if title and tok in title and len(tok) == len(title):
                continue
            counter[tok] += 1

    if not counter:
        return []

    most = counter.most_common(limit)
    max_c = most[0][1] if most else 1
    out: List[Dict[str, Any]] = []
    for text, cnt in most:
        weight = max(1, int(round(100.0 * cnt / max_c)))
        out.append({"text": text, "weight": weight, "category": "general"})
    return out


def heat_trend_from_daily(
    event_date: str,
    heat_by_date: Dict[str, int],
    *,
    before: int = 5,
    after: int = 3,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """由日热度字典生成 heat_trend / observation_window / heat 契约结构。"""
    ed = datetime.strptime(event_date, "%Y-%m-%d")
    start = ed - timedelta(days=before)
    end = ed + timedelta(days=after)
    points: List[Dict[str, Any]] = []
    for offset in range(-before, after + 1):
        d = (ed + timedelta(days=offset)).strftime("%Y-%m-%d")
        points.append(
            {
                "date": d,
                "offset_day": offset,
                "heat": int(heat_by_date.get(d, 0) or 0),
                "is_event_day": offset == 0,
            }
        )
    event_heat = next((p["heat"] for p in points if p["is_event_day"]), 0)
    prev = next((p["heat"] for p in points if p["offset_day"] == -1), None)
    change_rate = None
    direction = "unknown"
    if prev is not None and prev > 0:
        change_rate = round((event_heat - prev) / prev * 100, 1)
        direction = "up" if change_rate > 0 else ("down" if change_rate < 0 else "flat")
    elif prev == 0 and event_heat > 0:
        change_rate = None
        direction = "up"
    heat = {
        "total": int(event_heat),
        "comparison_base": "previous_day",
        "previous_value": prev,
        "change_rate": change_rate,
        "change_direction": direction,
    }
    window = {
        "before_days": before,
        "after_days": after,
        "start_date": start.strftime("%Y-%m-%d"),
        "event_date": event_date,
        "end_date": end.strftime("%Y-%m-%d"),
        "status": "active",
    }
    return points, window, heat


def fetch_same_period_heat(
    title: str,
    event_date: str,
    *,
    years_back: int = 3,
    query_fn: Optional[QueryFn] = None,
) -> List[Dict[str, Any]]:
    """历年同月日窗口热度（当天 ±0，单日合计）。无数据则 heat=0 仍返回年份。"""
    try:
        ed = datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        return []
    if not dm_enabled():
        return []

    out: List[Dict[str, Any]] = []
    mmdd = event_date[5:]
    for back in range(1, years_back + 1):
        yy = ed.year - back
        if yy < 1990:
            break
        try:
            hist = f"{yy}-{mmdd}"
            datetime.strptime(hist, "%Y-%m-%d")
        except ValueError:
            continue
        daily = fetch_daily_heat(
            title, hist, before=0, after=0, query_fn=query_fn
        )
        heat = int(daily.heat_by_date.get(hist, 0) or 0)
        out.append(
            {
                "year": yy,
                "heat": heat,
                "period_start": hist,
                "period_end": hist,
            }
        )
    return out
