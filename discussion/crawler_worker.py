"""Isolated MediaCrawler client runner. Never imports the Flask app or DB stores.

Only explicit login jobs open a visible browser. Crawl jobs use a saved local
session; challenges stop collection and are never solved automatically.
"""
import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MethodType
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from discussion.images import post_images
MEDIA = ROOT / "MindSpider/DeepSentimentCrawling/MediaCrawler"
PLATFORMS = {
    "dy": ("douyin", "DouYinClient", "https://www.douyin.com"),
    "xhs": ("xhs", "XiaoHongShuClient", "https://www.xiaohongshu.com"),
    "wb": ("weibo", "WeiboClient", "https://m.weibo.cn"),
}


class AccessRequired(Exception):
    pass


class CursorInvalid(Exception):
    pass


class RateLimited(Exception):
    pass


class ContentUnavailable(Exception):
    pass


class UnexpectedResponse(Exception):
    """The API returned no usable data, without explicit authentication failure."""


class CollectionStopped(Exception):
    def __init__(self, reason):
        self.reason = reason


def check_stop(client):
    if getattr(client, 'discussion_stop', Path('/nonexistent')).exists():
        raise CollectionStopped('user_stopped')


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def clean(value):
    return unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


def date_text(value):
    try:
        stamp = float(value)
        if stamp > 10**11:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            return parsedate_to_datetime(str(value)).isoformat()
        except (TypeError, ValueError, OverflowError):
            return str(value or "未提供")[:80]


def chrome_path(playwright):
    configured = os.environ.get("DISCUSSION_CRAWLER_CHROME")
    if configured:
        if not Path(configured).is_file():
            raise FileNotFoundError("browser")
        return configured
    if Path(playwright.chromium.executable_path).exists():
        return playwright.chromium.executable_path
    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    candidates = sorted(cache.glob("chromium-*/chrome-win*/chrome.exe"), reverse=True)
    if not candidates:
        raise FileNotFoundError("browser")
    return str(candidates[0])


def load_client(platform):
    os.chdir(MEDIA)
    sys.path.insert(0, str(MEDIA))
    import config
    config.ENABLE_IP_PROXY = False
    module, name, _ = PLATFORMS[platform]
    cls = getattr(importlib.import_module(f"media_platform.{module}.client"), name)
    logging.disable(logging.CRITICAL)
    return cls


async def request_once(self, method, url, **kwargs):
    """Keep existing platform signing; replace retries with bounded, honest errors."""
    import httpx
    for attempt in range(3):
        check_stop(self)
        await asyncio.sleep(2 if attempt == 0 else 2 ** attempt)
        check_stop(self)
        try:
            result = getattr(self, 'discussion_result', None)
            if result is not None:
                result['request_count'] = result.get('request_count', 0) + 1
                save(self.discussion_output, result)
            async with httpx.AsyncClient(follow_redirects=False) as http:
                # Bound each request, never the sum of detail/page requests.
                response = await asyncio.wait_for(http.request(method, url, timeout=20, **kwargs), timeout=22)
            if response.status_code >= 500 and attempt < 2:
                continue
            break
        except (httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError):
            if attempt == 2:
                raise
    result = getattr(self, 'discussion_result', None)
    if result is not None:
        # Allowlisted metadata only: never persist signed URLs or credentials here.
        result['response_diagnostic'] = dict(endpoint=urlsplit(url).path,
            http_status=response.status_code,
            content_type=response.headers.get('content-type', '').split(';')[0],
            body_bytes=len(response.content))
        save(self.discussion_output, result)
    if response.status_code == 429:
        raise RateLimited()
    if response.status_code in (301, 302, 401, 403, 461, 471):
        raise AccessRequired()
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        if result is not None:
            result['result_diagnostic'] = '接口返回空响应或非 JSON 内容，未确认登录失效；不能视为没有搜索结果'
        raise UnexpectedResponse() from None
    if not isinstance(data, dict):
        raise UnexpectedResponse()
    # Raw response bodies are private local artifacts, never sent to the UI or model.
    if getattr(self, 'discussion_output', None):
        raw_file = self.discussion_output.parent / ('response-' + str(result.get('request_count', 0)) + '.json')
        save(raw_file, data)
    message = str(data.get('msg') or data.get('message') or data.get('status_msg') or '').lower()
    if any(term in message for term in ('频繁', '频率', 'too many requests', 'rate limit')):
        raise RateLimited()
    if any(term in message for term in ('笔记不存在', '笔记已删除', '内容已删除', '微博不存在', '评论已关闭', '禁止评论', '评论不可见')):
        raise ContentUnavailable()
    if any(term in message for term in ('invalid cursor', 'cursor expired', 'invalid search_id', 'search_id expired', '游标失效', '游标无效')):
        raise CursorInvalid()
    if any(term in message for term in ('请登录', '请先登录', '登录失效', '登录过期', '未登录', '验证码', '安全验证', '访问受限', '访问异常', 'login required', 'verify required')):
        raise AccessRequired()
    if self.discussion_platform == "xhs":
        if not data.get("success"):
            if data.get('code') in (-100, -10001):
                raise AccessRequired()
            raise UnexpectedResponse()
        return data.get("data") or {}
    if self.discussion_platform == "wb":
        if data.get("ok") != 1:
            if data.get('ok') == 0 and (message.strip() in ('暂无数据', '暂无更多数据', '没有更多内容了')
                                       or message.startswith('还没有人评论哦')):
                return {'data': [], 'max_id': 0, 'cards': []}
            # A failed/deleted comment thread is not proof that every request on
            # the account needs a fresh login. Preserve other posts' work.
            raise UnexpectedResponse()
        return data.get("data") or {}
    if data.get("status_code", 0) != 0 or data.get("verify"):
        raise AccessRequired()
    return data


def write_post(result, item, platform, query):
    if platform == "dy":
        pid, text, title, date = item.get("aweme_id"), item.get("desc"), item.get("desc"), item.get("create_time")
        url = f"https://www.douyin.com/video/{pid}"
    elif platform == "xhs":
        pid, text, title, date = item.get("note_id"), item.get("desc"), item.get("title"), item.get("time")
        url = f"https://www.xiaohongshu.com/explore/{pid}"
    else:
        pid, text, title, date = item.get("id"), item.get("text"), "微博帖子", item.get("created_at")
        url = f"https://m.weibo.cn/detail/{pid}"
    images = post_images(item, platform)
    if not pid or not (clean(text) or clean(title) or images):
        return None
    pid = str(pid)
    if any(p["id"] == pid for p in result["posts"]):
        return None
    post = {"id": pid, "text": clean(text) or clean(title) or '图片帖（尚未识别图片内容）', "title": clean(title)[:160],
            "url": url, "date": date_text(date), "platform": platform, "query": query,
            "content_scope": 'image_only' if not (clean(text) or clean(title)) and images else item.get('content_scope', 'post_text'), "image_candidates": images}
    result["posts"].append(post)
    return post


async def collect(client, spec, result, output):
    """One search or first-level comment page, with durable resume state."""
    platform, query = spec["platform"], spec["query"]
    cursor = dict(spec.get('cursor') or {})
    result['cursor'] = cursor
    kind = spec.get('kind', 'search')
    if kind == 'comments':
        post = spec['post']
        result['parents'] = [post]
        if platform == 'dy':
            data = await client.get_aweme_comments(post['id'], cursor=int(cursor.get('next', 0)))
            items = data.get('comments')
            nxt, more = data.get('cursor'), data.get('has_more')
        elif platform == 'xhs':
            data = await client.get_note_comments(post['id'], post.get('token', ''), cursor=str(cursor.get('next', '')))
            items = data.get('comments')
            nxt, more = data.get('cursor'), data.get('has_more')
        else:
            data = await client.get_note_comments(post['id'], int(cursor.get('next', 0)), int(cursor.get('max_id_type', 0)))
            items = data.get('data')
            nxt = data.get('max_id')
            more = nxt not in (None, '', 0, '0') if 'max_id' in data else None
            cursor['max_id_type'] = data.get('max_id_type', 0)
        if not isinstance(items, list) or more not in (True, False, 0, 1, '0', '1'):
            result['result_diagnostic'] = '评论响应缺少有效列表或分页标记，断点保留；不能当作评论已读完'
            raise UnexpectedResponse()
        more = more in (True, 1, '1')
        if more and nxt in (None, ''):
            result['result_diagnostic'] = '评论响应声明有下一页但未提供游标，保留断点'
            raise UnexpectedResponse()
        for item in items:
            cid = str(item.get('cid') or item.get('id') or '')
            text = clean(item.get('text') or item.get('content'))
            if cid and text:
                result['comments'].append(dict(id=cid, text=text, parent_id=post['id'], date=date_text(item.get('create_time') or item.get('created_at') or item.get('time'))))
        repeated = more and str(nxt) == str(cursor.get('next', '' if platform == 'xhs' else 0))
        if platform == 'wb':
            repeated = repeated and str(cursor['max_id_type']) == str((spec.get('cursor') or {}).get('max_id_type', 0))
        result.update(has_more=more, repeated_cursor=repeated, page=int(cursor.get('page', 0)) + 1)
        cursor.update(next=nxt, page=result['page'])
    else:
        # A partially processed XHS search page can resume note details without fetching it again.
        if 'pending' in cursor:
            items = cursor['pending']
            nxt, more = cursor.get('pending_next'), cursor.get('pending_more', False)
        elif platform == 'dy':
            from var import request_keyword_var
            from media_platform.douyin.field import SearchChannelType
            request_keyword_var.set(query)
            data = await client.search_info_by_keyword(query, offset=int(cursor.get('next', 0)), search_id=cursor.get('search_id', ''), search_channel=SearchChannelType.VIDEO)
            if 'data' not in data and 'aweme_list' not in data:
                result['result_diagnostic'] = '搜索响应缺少帖子列表字段，可能是接口结构变化；不能视为没有结果或登录失效'
                raise UnexpectedResponse()
            items = []
            rows = data.get('data') if isinstance(data.get('data'), list) else data.get('aweme_list')
            if not isinstance(rows, list):
                raise UnexpectedResponse()
            for item in rows:
                if isinstance(item, dict):
                    if item.get('aweme_id'):
                        items.append(item)
                    elif item.get('aweme_info'):
                        items.append(item['aweme_info'])
                    elif item.get('aweme_mix_info'):
                        items.extend(item['aweme_mix_info'].get('mix_items') or [])
            if rows and not items:
                raise UnexpectedResponse()
            nxt, more = data.get('cursor', int(cursor.get('next', 0)) + 15), bool(data.get('has_more'))
            if not items:
                result['result_diagnostic'] = '接口本次返回空列表；仅表示此查询没有返回帖子，不能推断平台没有相关内容'
            cursor['search_id'] = (data.get('log_pb') or {}).get('impr_id', cursor.get('search_id', ''))
        elif platform == 'xhs':
            from uuid import uuid4
            cursor.setdefault('search_id', uuid4().hex)
            data = await client.get_note_by_keyword(query, page=int(cursor.get('next', 1)), search_id=cursor['search_id'])
            items = [x for x in data.get('items', []) if x.get('model_type') not in ('rec_query', 'hot_query')]
            nxt, more = int(cursor.get('next', 1)) + 1, bool(data.get('has_more'))
        else:
            data = await client.get_note_by_keyword(query, page=int(cursor.get('next', 1)))
            items = []
            for card in data.get('cards', []):
                for node in [card] + card.get('card_group', []):
                    if node.get('mblog'):
                        items.append(node['mblog'])
            nxt, more = int(cursor.get('next', 1)) + 1, bool(items)
        cursor.update(pending=list(items), pending_next=nxt, pending_more=more)
        save(output, result)
        while cursor['pending']:
            check_stop(client)
            item = cursor['pending'][0]
            token = ''
            try:
                if platform == 'xhs':
                    token = item.get('xsec_token', '')
                    detail = await client.get_note_by_id(item.get('id'), item.get('xsec_source', 'pc_search'), token)
                    if not detail or not detail.get('note_id'):
                        raise ContentUnavailable()
                    item = detail
                elif platform == 'wb' and item.get('isLongText'):
                    detail = await client.get('/statuses/extend', {'id': str(item['id'])})
                    if not detail.get('longTextContent'):
                        raise ContentUnavailable()
                    item = dict(item, text=detail['longTextContent'])
            except (CollectionStopped, AccessRequired, RateLimited, CursorInvalid):
                # Platform-wide restrictions stop requests; do not try each remaining post.
                raise
            except Exception as exc:
                # A deleted post or failed detail must not discard other search hits.
                result.setdefault('item_failures', []).append(dict(
                    post_id=str(item.get('id', '')), error_type=type(exc).__name__,
                    message='详情未取得，保留可用搜索摘要并继续其他帖子'))
                if platform == 'xhs':
                    card = item.get('note_card') or {}
                    item = dict(note_id=item.get('id'), title=card.get('display_title') or card.get('title'),
                                desc=card.get('desc'), time=card.get('time'), content_scope='search_summary',
                                image_list=[card['cover']] if isinstance(card.get('cover'), dict) else [])
                else:
                    item = dict(item, content_scope='search_summary')
            post = write_post(result, item, platform, query)
            if post and token:
                post['token'] = token
            cursor['pending'].pop(0)
            save(output, result)
        result.update(has_more=more, repeated_cursor=more and nxt == cursor.get('next'), page=int(cursor.get('page', 0)) + 1)
        cursor.pop('pending', None)
        cursor.pop('pending_next', None)
        cursor.pop('pending_more', None)
        cursor.update(next=nxt, page=result['page'])
    result.update(status='complete', message='本页已保存', cursor=cursor)
    save(output, result)


async def run(spec, output, result):
    from playwright.async_api import async_playwright
    platform = spec["platform"]
    profile = Path(spec["profile"])
    profile.mkdir(parents=True, exist_ok=True)
    session = profile / "session.json"
    login = spec.get("action") == "login"
    if not login and not session.exists():
        result.update(status="needs_login", message="尚未连接平台，请先点击登录，完成后再开始一轮。")
        return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not login, executable_path=chrome_path(p))
        try:
            context = await browser.new_context(storage_state=str(session) if session.exists() else None,
                                                viewport={"width": 1280, "height": 850})
            page = await context.new_page()
            target = PLATFORMS[platform][2]
            if platform == "dy" and spec.get("query"):
                target += "/search/" + quote(spec["query"], safe="") + "?type=video"
            await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            if login:
                result.update(status="login_open", message="请在打开的浏览器中登录，然后回到工作台点击“保存登录”。")
                save(output, result)
                end = time.monotonic() + 240
                while time.monotonic() < end and not page.is_closed():
                    if (output.parent / "finish").exists():
                        # Browser credentials stay in this ignored server-side directory.
                        save(session, await context.storage_state())
                        result.update(status="session_saved", message="会话已保存；是否能采集将由下一轮实际请求确认。")
                        return
                    await asyncio.sleep(1)
                result.update(status="needs_login", message="登录窗口已关闭或超时，未保存新的会话。")
                return
            await page.wait_for_timeout(2000)
            page_title = await page.title()
            if any(word in page_title for word in ("验证码", "安全验证")):
                raise AccessRequired()
            cls = load_client(platform)
            cookies = {c["name"]: c["value"] for c in await context.cookies(PLATFORMS[platform][2])}
            client = cls(timeout=20, headers={"User-Agent": await page.evaluate("navigator.userAgent"),
                         "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
                         "Referer": PLATFORMS[platform][2] + "/", "Origin": PLATFORMS[platform][2],
                         "Content-Type": "application/json;charset=UTF-8"}, playwright_page=page, cookie_dict=cookies)
            client.discussion_platform = platform
            client.discussion_stop = output.parent / "stop"
            client.discussion_result = result
            client.discussion_output = output
            client.request = MethodType(request_once, client)
            # The upstream Douyin pong checks obsolete LOGIN_STATUS/HasUserLogin
            # flags, even when a valid sessionid exists. Let the real API validate
            # the user's saved session instead of rejecting it locally.
            await collect(client, spec, result, output)
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    args = parser.parse_args()
    spec = json.loads(args.job.read_text(encoding="utf-8"))
    output = args.job.parent / "result.json"
    result = {"status": "running", "message": "正在连接平台", "posts": [], "comments": []}
    save(output, result)
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        result.update(status="unavailable", message="缺少 filelock，请安装 requirements.crawler.txt")
        save(output, result)
        return
    try:
        profile = Path(spec["profile"])
        profile.mkdir(parents=True, exist_ok=True)
        with FileLock(str(profile / "session.lock"), timeout=0):
            if spec.get('action') == 'login':
                asyncio.run(asyncio.wait_for(run(spec, output, result), timeout=290))
            else:
                asyncio.run(run(spec, output, result))
    except CollectionStopped as exc:
        result.update(status=exc.reason, message="用户停止采集，断点已保存")
    except RateLimited:
        result.update(status='rate_limited', message='平台限制访问频率，已暂停该平台；断点保留，不循环重试。')
    except AccessRequired:
        result.update(status="needs_login", message="平台拒绝访问或要求登录/验证。请打开登录窗口，在事件搜索页完成平台验证后保存，再重试；本次已停止请求。")
    except ContentUnavailable:
        result.update(status='content_unavailable', error_type='ContentUnavailable',
                      message='当前帖子或评论已删除、关闭或不可见；跳过此帖，继续其他可用帖子。')
    except UnexpectedResponse:
        result.update(status='failed', error_type='UnexpectedResponse',
                      message='平台接口未返回可解析数据，未确认登录失效；保留断点，不按无结果处理。')
    except CursorInvalid:
        result.update(status='cursor_invalid', message='平台明确报告分页断点失效，重新查询后去重')
    except Timeout:
        result.update(status="busy", message="该平台正在登录或被另一场会议采集，请稍后重试。")
    except (asyncio.TimeoutError, TimeoutError):
        result.update(status="timeout", message="采集超时，已停止；已取得的数据保留，下一轮可重试。")
    except (ImportError, FileNotFoundError):
        result.update(status="unavailable", message="爬虫依赖或 Chromium 不完整，请按本地启动说明补齐。")
    except Exception as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        diagnostic = f"HTTP {code}" if isinstance(code, int) else type(exc).__name__
        result.update(status="failed", error_type=type(exc).__name__, message=f"平台请求未完成（{diagnostic}），可能是网络或接口变化；可重新连接后重试。")
    if (result["posts"] or result["comments"]) and result["status"] not in ("complete", "empty"):
        result["failure_status"] = result["status"]
        result["status"] = "partial"
    save(output, result)


if __name__ == "__main__":
    main()
