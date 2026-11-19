"""Download post attachments without a model; original bytes remain immutable.

Only platform image CDNs are fetched. Image content is never inferred from a
URL, filename or accompanying post text. Extraction is handled by vision.py.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from .store import now
from .webpages import public_url

CDNS = {'wb': ('sinaimg.cn',), 'xhs': ('xhscdn.com', 'snsimg.cn')}
ERRORS = {
    'blocked_url': '图片地址不是受支持的平台图片域名',
    'access_denied': '图片链接拒绝访问或已过期',
    'http_error': '图片服务器返回错误', 'timeout': '单张图片请求超时',
    'network_error': '图片网络请求失败', 'too_large': '图片超过单文件 20 MB 安全限制',
    'invalid_image': '返回内容不是受支持的图片',
    'too_many_pixels': '图片超过 4000 万像素，未解码',
    'stopped': '已停止，图片尚未下载', 'redirect_limit': '图片重定向次数异常',
}


def image_url(value, platform):
    if not isinstance(value, str):
        return ''
    value = 'https:' + value if value.startswith('//') else value
    try:
        u = urlsplit(value)
        if (u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password
                or u.port not in (None, 80, 443)
                or not any(u.hostname == h or u.hostname.endswith('.' + h) for h in CDNS.get(platform, ()))):
            return ''
    except ValueError:
        return ''
    return value


def post_images(item, platform):
    """Keep actual post images, never avatars or unrelated recommendation media."""
    groups = []
    if platform == 'wb':
        pics = item.get('pics') or list((item.get('pic_infos') or {}).values())
        groups.append((pics, str(item.get('id') or ''), '帖子配图'))
        quoted = item.get('retweeted_status') or {}
        if quoted.get('id'):
            groups.append((quoted.get('pics') or list((quoted.get('pic_infos') or {}).values()),
                           str(quoted['id']), '转发所附原帖配图'))
    elif platform == 'xhs':
        groups.append((item.get('image_list') or [], str(item.get('note_id') or item.get('id') or ''),
                       '搜索封面（详情未取得）' if item.get('content_scope') == 'search_summary' else '帖子配图'))
    result, seen = [], set()
    for pictures, origin, kind in groups:
        for pic in pictures:
            if not isinstance(pic, dict):
                continue
            candidates = [(pic.get('large') or {}).get('url'), pic.get('url_default'), pic.get('url')]
            candidates += [x.get('url') for x in pic.get('info_list', []) if isinstance(x, dict)]
            candidates.append(pic.get('url_pre'))
            urls = list(dict.fromkeys(u for value in candidates if (u := image_url(value, platform))))
            if not urls or urls[0] in seen:
                continue
            seen.add(urls[0])
            key = hashlib.sha256((origin + '|' + str(pic.get('pid') or pic.get('file_id') or urls[0])).encode()).hexdigest()[:24]
            result.append(dict(id='img-' + key, index=len(result) + 1, urls=urls,
                               origin_post_id=origin, kind=kind))
    return result


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def merge_image(previous, current):
    """A fresh download record must not erase extraction of identical bytes."""
    result = dict(current)
    if previous.get('sha256') and previous['sha256']==current.get('sha256'):
        result.update({k:v for k,v in previous.items() if k.startswith('analysis_') or k=='analyzed_at'})
    return result


def meeting_images(root, mid):
    if not re.fullmatch(r'meeting-[a-f0-9]{12}', mid):
        raise ValueError('会议编号无效')
    return Path(root).resolve() / mid / 'images'


class ImageDownloadError(Exception):
    pass


class ImageCollector:
    def __init__(self, root, mid, stopped=lambda: False):
        self.folder = meeting_images(root, mid)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.stopped = stopped

    def _download(self, url, platform):
        deadline = time.monotonic() + 45  # Per file only, never a whole collection budget.
        with requests.Session() as session:
            session.trust_env = False
            for _ in range(5):
                if self.stopped():
                    raise ImageDownloadError('stopped')
                if time.monotonic() >= deadline:
                    raise ImageDownloadError('timeout')
                if not image_url(url, platform):
                    raise ImageDownloadError('blocked_url')
                try:
                    public_url(url)
                except (ValueError, OSError):
                    raise ImageDownloadError('blocked_url') from None
                headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.xiaohongshu.com/' if platform == 'xhs' else 'https://weibo.com/'}
                with session.get(url, headers=headers, timeout=(8, 12), stream=True, allow_redirects=False) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get('Location', ''))
                        continue
                    if response.status_code in (401, 403, 404, 410, 429):
                        raise ImageDownloadError('access_denied')
                    if response.status_code != 200:
                        raise ImageDownloadError('http_error')
                    if int(response.headers.get('Content-Length') or 0) > 20 * 1024 * 1024:
                        raise ImageDownloadError('too_large')
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        if self.stopped():
                            raise ImageDownloadError('stopped')
                        if time.monotonic() >= deadline:
                            raise ImageDownloadError('timeout')
                        body.extend(chunk)
                        if len(body) > 20 * 1024 * 1024:
                            raise ImageDownloadError('too_large')
                    return bytes(body), url
        raise ImageDownloadError('redirect_limit')

    def fetch(self, candidate, platform, retry=False):
        key = candidate['id']
        if not re.fullmatch(r'img-[a-f0-9]{24}', key):
            raise ValueError('图片编号无效')
        metadata = self.folder / (key + '.json')
        if metadata.exists():
            saved = json.loads(metadata.read_text(encoding='utf-8'))
            sha = saved.get('sha256', '')
            if saved.get('status') == 'downloaded' and (self.folder / (sha + '.original')).is_file() and (self.folder / (sha + '.jpg')).is_file():
                return dict(saved, index=candidate['index'], kind=candidate['kind'], origin_post_id=candidate['origin_post_id'])
            if not retry:
                return saved
        record = {k: candidate[k] for k in ('id', 'index', 'origin_post_id', 'kind')}
        record.update(status='failed', analysis_status='not_analyzed', collected_at=now())
        code = 'blocked_url'
        for url in candidate.get('urls', []):
            try:
                raw, final_url = self._download(url, platform)
                with Image.open(io.BytesIO(raw)) as picture:
                    if picture.format not in ('JPEG', 'PNG', 'WEBP', 'GIF', 'AVIF'):
                        raise ImageDownloadError('invalid_image')
                    width, height = picture.size
                    if width * height > 40_000_000:
                        raise ImageDownloadError('too_many_pixels')
                    picture.load()
                    preview = ImageOps.exif_transpose(picture).convert('RGB')
                    preview.thumbnail((1600, 1600))
                    b = io.BytesIO()
                    preview.save(b, format='JPEG', quality=88)
                    fmt, frames = picture.format, getattr(picture, 'n_frames', 1)
                sha = hashlib.sha256(raw).hexdigest()
                for suffix, content in (('.original', raw), ('.jpg', b.getvalue())):
                    target = self.folder / (sha + suffix)
                    if not target.exists():
                        tmp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
                        tmp.write_bytes(content)
                        os.replace(tmp, target)
                record.update(status='downloaded', sha256=sha, width=width, height=height, size=len(raw),
                              format=fmt, frames=frames, source_url=final_url,
                              preview_note='静态首帧预览' if frames > 1 else '缩略预览；下载可获取原始文件')
                break
            except ImageDownloadError as exc:
                code = str(exc)
            except requests.Timeout:
                code = 'timeout'
            except requests.RequestException:
                code = 'network_error'
            except Image.DecompressionBombError:
                code = 'too_many_pixels'
            except (UnidentifiedImageError, OSError, ValueError):
                code = 'invalid_image'
            # Do not repeatedly challenge a denied CDN or retry a user stop.
            if code in ('stopped', 'access_denied'):
                break
        if record['status'] != 'downloaded':
            record.update(status='stopped' if code == 'stopped' else 'failed', error_code=code, message=ERRORS[code])
        write_json(metadata, record)
        return record

    def collect_posts(self, posts, platform, retry=False, progress=lambda *_: None):
        for post in posts:
            records = []
            for candidate in post.get('image_candidates', []):
                if self.stopped():
                    records.append(dict({k: candidate[k] for k in ('id', 'index', 'origin_post_id', 'kind')},
                                        status='stopped', analysis_status='not_analyzed', message=ERRORS['stopped']))
                else:
                    records.append(self.fetch(candidate, platform, retry=retry))
                post['images'] = records
                progress(post, records[-1])
            post['images'] = records
        return posts


def saved_candidates(root, mid, evidence):
    """Recover image fields from private responses; never return cookies/profiles."""
    wanted = {e['source_id']: e for e in evidence if e.get('source_type') == 'social_post' and e.get('platform') in CDNS}
    found = {}
    def visit(value, platform):
        if isinstance(value, dict):
            pid = str(value.get('note_id') if platform == 'xhs' else value.get('id') or '')
            sid = f'{platform}:post:{pid}'
            if sid in wanted and (value.get('image_list') if platform == 'xhs' else value.get('pics') or value.get('pic_infos') or value.get('retweeted_status')):
                dest = found.setdefault(sid, {})
                for candidate in post_images(value, platform):
                    dest.setdefault(candidate['id'], candidate)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child, platform)
        elif isinstance(value, list):
            for child in value:
                visit(child, platform)
    folder = meeting_images(root, mid).parent / 'crawls'
    for page in sorted(folder.glob('*')):
        try:
            job = json.loads((page / 'job.json').read_text(encoding='utf-8'))
            platform = job.get('platform')
            if platform not in CDNS:
                continue
            for response in sorted(page.glob('response-*.json')):
                visit(json.loads(response.read_text(encoding='utf-8')), platform)
        except (OSError, ValueError):
            continue
    for sid, choices in found.items():
        for n, item in enumerate(choices.values(), 1):
            item['index'] = n
    return {sid: list(values.values()) for sid, values in found.items()}


def attachment_path(root, mid, sha, original=False):
    if not re.fullmatch(r'[a-f0-9]{64}', sha):
        raise KeyError('图片不存在')
    path = meeting_images(root, mid) / (sha + ('.original' if original else '.jpg'))
    if not path.is_file():
        raise KeyError('图片不存在')
    return path
