"""Meeting-scoped social collection, subprocess limits and local login sessions."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .evidence import make_evidence
from .store import Conflict, now, uid

PLATFORMS = {"dy": "抖音", "xhs": "小红书", "wb": "微博"}
SCOPE = "由协调智能体按问题决定分页、换词和结束，不设整轮时限或帖子总数上限；采集帖子文字、一级评论及微博/小红书配图。相关已保存配图可经视觉模型提取后参与研讨，不含视频和二级评论。"


def platforms(value):
    if not isinstance(value, list) or any(not isinstance(p, str) or p not in PLATFORMS for p in value):
        raise ValueError("采集平台仅支持抖音、小红书、微博")
    return list(dict.fromkeys(value))


def normalize(result, platform, query, collected_at):
    """Allowlisted fields only; secrets/user profiles never enter evidence or prompts."""
    evidence, comments, parents, ids = [], [], {}, set()
    for post in result.get("posts", []):
        pid = str(post.get("id", ""))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", pid) or pid in parents:
            continue
        text = str(post.get("text", "")).strip()
        if not text:
            continue
        url = {"dy": "https://www.douyin.com/video/", "xhs": "https://www.xiaohongshu.com/explore/", "wb": "https://m.weibo.cn/detail/"}[platform] + pid
        parents[pid] = url
        sid = f"{platform}:post:{pid}"
        item = make_evidence(text, str(post.get("title") or PLATFORMS[platform] + "帖子")[:160],
                             PLATFORMS[platform] + " · 帖子文字（不含视频/图片内容）", "social_post", url,
                             str(post.get("date", ""))[:80], sid)
        item.update(platform=platform, collected_at=collected_at, query=query,
                    content_scope=post.get('content_scope', 'post_text'))
        if post.get('images'):
            item['images'] = post['images']
        if post.get('content_scope') == 'search_summary':
            item['locator'] = PLATFORMS[platform] + ' · 搜索结果摘要（详情未取得）'
        evidence.append(item)
    for parent in result.get("parents", []):
        pid = str(parent.get("id", ""))
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", pid):
            parents[pid] = {"dy": "https://www.douyin.com/video/", "xhs": "https://www.xiaohongshu.com/explore/", "wb": "https://m.weibo.cn/detail/"}[platform] + pid
    per_post = {}
    for c in result.get("comments", []):
        parent, cid = str(c.get("parent_id", "")), str(c.get("id", ""))
        text = str(c.get("text", "")).strip()
        if parent not in parents or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", cid) or not text or cid in ids:
            continue
        ids.add(cid)
        per_post[parent] = per_post.get(parent, 0) + 1
        sid = f"{platform}:comment:{cid}"
        evidence_item = make_evidence(text, PLATFORMS[platform] + "评论样本", f"一级评论 {cid} · 所属帖子 {parent}",
                                      "social_comment", parents[parent], str(c.get("date", ""))[:80], sid)
        evidence_item.update(platform=platform, collected_at=collected_at, query=query)
        evidence.append(evidence_item)
        comments.append({"id": sid, "text": text, "time": evidence_item["date"], "source": PLATFORMS[platform],
                         "platform": platform, "parent_id": parent, "url": parents[parent],
                         "evidence_id": evidence_item["id"], "collected_at": collected_at})
    return evidence, comments


class CrawlerBridge:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.Lock()
        self.logins = {}

    def _spawn(self, folder, platform, action, query="", extra=None):
        folder.mkdir(parents=True, exist_ok=False)
        spec = {"platform": platform, "query": query[:120], "action": action,
                "profile": str(self.root / "crawler_sessions" / platform)}
        spec.update(extra or {})
        job = folder / "job.json"
        job.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        bundled_node = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin"
        if bundled_node.exists():
            env["PATH"] = str(bundled_node) + os.pathsep + env.get("PATH", "")
        # No shell; no secrets in argv; suppress upstream logs that can contain cookies.
        return subprocess.Popen([sys.executable, str(Path(__file__).with_name("crawler_worker.py")), str(job)],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                start_new_session=os.name != "nt")

    @staticmethod
    def _kill(process):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    @staticmethod
    def _result(folder):
        try:
            return json.loads((folder / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "running", "message": "正在启动平台连接", "posts": [], "comments": []}

    def collect_page(self, mid, round_number, task, idle_timeout=90, stopped=lambda: False):
        platform, query = task['platform'], task['query']
        platforms([platform])
        if not re.fullmatch(r"meeting-[a-f0-9]{12}", mid):
            raise ValueError("会议编号无效")
        folder = self.root / mid / "crawls" / uid(f"round-{int(round_number)}-{platform}")
        extra = dict(kind=task.get('kind', 'search'), cursor=task.get('cursor', {}),
                     post=task.get('post'), request_timeout=20)
        process = self._spawn(folder, platform, "crawl", query, extra)
        # Only lack of progress is bounded. A page with many successful detail
        # requests may take arbitrarily long; each saved response resets this clock.
        progress_at, signature, stop_at = time.monotonic(), None, None
        while True:
            if stopped():
                (folder / 'stop').touch()
                if stop_at is None:
                    stop_at = time.monotonic()
            try:
                process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                current = time.monotonic()
                try:
                    stat = (folder / 'result.json').stat()
                    latest = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    latest = None
                if latest != signature:
                    signature, progress_at = latest, current
                if current-progress_at >= max(30, idle_timeout) or (stop_at is not None and current-stop_at >= 25):
                    self._kill(process)
                    break
        result = self._result(folder)
        if result['status'] == 'running':
            result.update(status='user_stopped' if stop_at is not None else 'timeout',
                          message='用户停止采集，断点已保存' if stop_at is not None else '连接持续无进展；已取得的数据和断点保留')
        # Raw pages and private cursor metadata stay server-side, never in public task fields.
        result['raw_page'] = str(folder.relative_to(self.root))
        if platform in ('wb', 'xhs') and any(p.get('image_candidates') for p in result.get('posts', [])):
            from .images import ImageCollector, write_json
            images = ImageCollector(self.root, mid, stopped)
            images.collect_posts(result['posts'], platform)
            # Persist even if the subsequent LLM relevance step fails.
            write_json(folder / 'result.json', result)
        stamp = now()
        evidence, comments = normalize(result, platform, query, stamp)
        return result, evidence, comments

    def collect(self, mid, round_number, platform, query):
        # Compatibility entry for callers requesting a single batch.
        result, evidence, comments = self.collect_page(mid, round_number, dict(platform=platform, query=query), 300)
        record = dict(platform=platform, platform_name=PLATFORMS[platform], query=query,
                      status=result['status'], message=result['message'], scope=SCOPE,
                      collected_at=now(), post_count=sum(e['source_type']=='social_post' for e in evidence), comment_count=len(comments))
        return record, evidence, comments

    def connections(self):
        items = []
        with self.lock:
            for p, name in PLATFORMS.items():
                job = self.logins.get(p)
                status = "session_saved" if (self.root / "crawler_sessions" / p / "session.json").exists() else "needs_login"
                message = "已保存本机会话，采集时验证有效性" if status == "session_saved" else "尚未连接"
                if job:
                    result = self._result(job[1])
                    status, message = result["status"], result["message"]
                    if job[0].poll() is not None and status in ("running", "login_open"):
                        status, message = "failed", "登录进程已结束，请重新连接"
                items.append({"platform": p, "name": name, "status": status, "message": message})
        return items

    def login(self, platform, finish=False, query=""):
        platforms([platform])
        with self.lock:
            current = self.logins.get(platform)
            if finish:
                if not current or current[0].poll() is not None:
                    raise ValueError("登录窗口未打开或已超时，请重新登录")
                (current[1] / "finish").touch()
            else:
                if current and current[0].poll() is None:
                    raise Conflict("该平台已有登录窗口，请在窗口中完成登录")
                folder = self.root / "crawler_sessions" / platform / uid("login")
                process = self._spawn(folder, platform, "login", query)
                self.logins[platform] = (process, folder)
                def reap():
                    try:
                        process.wait(timeout=305)
                    except subprocess.TimeoutExpired:
                        self._kill(process)
                threading.Thread(target=reap, daemon=True).start()
        return self.connections()
