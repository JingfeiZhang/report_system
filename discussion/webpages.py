"""Fetch public search-result pages automatically; keep summaries on failure."""
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlsplit, urljoin

import requests
from bs4 import BeautifulSoup

from .evidence import chunks, make_evidence
from .store import now


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('unsupported_url')
    if parsed.port not in (None, 80, 443):
        raise ValueError('unsupported_port')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme=='https' else 80))
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('non_public_address')


def extract_article(body):
    """Extract prose containers, not an entire navigation-heavy body fallback."""
    soup = BeautifulSoup(body, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    if any(word in title for word in ('验证码', '安全验证', '访问验证')) or title.startswith(('登录', '请登录')):
        return '', 'access_page'
    structured = []
    def article_bodies(value):
        if isinstance(value, dict):
            if isinstance(value.get('articleBody'), str):
                structured.append(BeautifulSoup(value['articleBody'], 'html.parser').get_text('\n', strip=True))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    article_bodies(child)
        elif isinstance(value, list):
            for child in value:
                article_bodies(child)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            article_bodies(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError, RecursionError):
            continue
    for node in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form']):
        node.decompose()
    for node in soup.select('.related, .recommend, .recommendations, .share, .comments, .breadcrumb, .navigation'):
        node.decompose()
    def prose(node, minimum):
        text = '\n'.join(line.strip() for line in node.get_text('\n').splitlines() if line.strip())
        links = sum(len(a.get_text(strip=True)) for a in node.select('a'))
        if len(text) < minimum or links > len(text)*0.35 or not re.search(r'[。！？.!?；;]', text):
            return ''
        if any(t in text for t in ('请完成安全验证', '请开启JavaScript并刷新', '登录后查看全文')):
            return ''
        return text
    selectors = ('[itemprop="articleBody"], article, .article-body, .article-content, .main_content, '
                 '.content_area, #content_area, .TRS_Editor, #articleContent, #artical_real, '
                 '.post-content, .entry-content, #js_content, .cnt_bd')
    candidates = [prose(node, 40) for node in soup.select(selectors)]
    candidates.extend(text for text in structured if len(text)>=40 and re.search(r'[。！？.!?；;]', text))
    candidates = [text for text in candidates if text]
    if candidates:
        return max(candidates, key=len), 'article_container'
    # Div-based articles and short notices are common. Collect only substantial
    # prose blocks; menus/recommendation links never enter this fallback.
    blocks = []
    for node in soup.select('p, div'):
        if node.select_one('p, div') is not None:
            continue
        text = prose(node, 60)
        if text and text not in blocks:
            blocks.append(text)
    text = '\n'.join(blocks)
    return (text, 'prose_blocks') if len(text)>=120 else ('', 'no_extractable_article')


def render_article(url, stopped, on_request):
    """Read a public JS-rendered article in a fresh, credential-free browser.

    Browser navigation is a bounded request operation; it is not a collection
    deadline. Every HTTP target is checked, including subresources/redirects.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    from .crawler_worker import chrome_path
    checked = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=chrome_path(p))
        try:
            context = browser.new_context(service_workers='block', accept_downloads=False)
            def guard(route):
                req = route.request
                if stopped() or req.resource_type in ('image', 'media', 'font') or req.method not in ('GET', 'HEAD'):
                    return route.abort()
                try:
                    parsed = urlsplit(req.url)
                    # Validate scheme/authority on every URL; cache only DNS checks.
                    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,80,443):
                        return route.abort()
                    origin = (parsed.scheme,parsed.hostname,parsed.port)
                    if origin not in checked:
                        public_url(req.url)
                        checked.add(origin)
                except (OSError, ValueError):
                    return route.abort()
                on_request()
                return route.continue_()
            context.route('**/*', guard)
            context.route_web_socket('**/*', lambda ws: ws.close())
            page = context.new_page()
            try:
                response = page.goto(url, wait_until='domcontentloaded', timeout=20000)
                if response and response.status != 200:
                    return '', 'render_http_error', page.url
            except BrowserTimeout:
                # The document can already contain useful text despite a slow asset.
                pass
            deadline = time.monotonic()+10
            while not stopped():
                text, method = extract_article(page.content())
                if text or method=='access_page':
                    return text, ('browser_'+method if text else method), page.url
                if time.monotonic() >= deadline:
                    return '', 'dynamic_content_unavailable', page.url
                page.wait_for_timeout(500)
            return '', 'stopped', page.url
        finally:
            browser.close()


def fetch_page(source, seconds=20, stopped=lambda:False, on_request=lambda:None):
    url, deadline = source['url'], time.monotonic()+seconds
    record = dict(url=url, status='pending', fetched_at=now(), chunks=0)
    try:
        with requests.Session() as session:
            session.trust_env = False
            for _ in range(5):
                if stopped() or time.monotonic()>=deadline:
                    record['status']='stopped' if stopped() else 'request_timeout'
                    return [], record, ''
                public_url(url)
                on_request()
                with session.get(url, timeout=max(1,min(10,deadline-time.monotonic())),
                                 allow_redirects=False, stream=True,
                                 headers={'User-Agent':'Mozilla/5.0 (compatible; DiscussionResearch/1.0)'}) as response:
                    record['http_status']=response.status_code
                    if response.is_redirect:
                        url=urljoin(url,response.headers.get('Location',''))
                        continue
                    if response.status_code!=200:
                        record['status']='http_error'
                        return [],record,''
                    if 'html' not in response.headers.get('Content-Type','').lower():
                        record['status']='unsupported_content'
                        return [],record,''
                    body=bytearray()
                    for block in response.iter_content(16384):
                        if stopped() or time.monotonic()>=deadline:
                            record['status']='stopped' if stopped() else 'request_timeout'
                            return [],record,''
                        body.extend(block)
                        if len(body)>2*1024*1024:
                            record['status']='page_too_large'
                            return [],record,''
                    break
            else:
                record['status']='redirect_limit'
                return [],record,''
        text, method = extract_article(bytes(body))
        if not text and method=='no_extractable_article' and not stopped():
            markup = BeautifulSoup(bytes(body), 'html.parser')
            # Empty article containers and application roots indicate deferred
            # content. Do not launch a browser for ordinary link directories.
            dynamic = markup.find('script') is not None and markup.select_one(
                'article, #content_area, .article-content, #articleContent, #root, #app, #__next') is not None
            if dynamic:
                record['static_status'] = method
                try:
                    text, method, url = render_article(url, stopped, on_request)
                except Exception as exc:
                    record.update(status='render_failed',error_type=type(exc).__name__)
                    return [],record,''
        if not text:
            record['status']=method
            return [],record,''
        evidence=[]
        for i, part in enumerate(chunks(text, size=1200, overlap=100)):
            e=make_evidence(part,source['title'],f'自动提取网页文字 · 片段 {i+1}（不含图片，需核对来源归属）',
                            'web_page',url,source.get('date',''),source['url']+f'#text-{i+1}')
            e.update(fetched_at=record['fetched_at'], parent_summary_id=source['id'], extraction=method)
            evidence.append(e)
        record.update(status='fetched',final_url=url,chunks=len(evidence))
        return evidence,record,text
    except (requests.RequestException, OSError, ValueError) as exc:
        record.update(status='request_timeout' if isinstance(exc, requests.Timeout) else 'fetch_failed',error_type=type(exc).__name__)
        return [],record,''
