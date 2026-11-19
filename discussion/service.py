"""Round orchestration. A frozen round input prevents mid-round instruction drift."""
from __future__ import annotations

import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import demo
from .crawler import CrawlerBridge, PLATFORMS, SCOPE, platforms
from .evidence import parse_upload
from .providers import LiveProviders
from .debate import DebateRunner, evidence_context, configure_collaboration
from .store import Conflict, now, uid
from .collection import RoundCollector, public_task
from .sampling import classify
from . import checkpoints, report_checks

ROLES = {"facts": "事实研究", "media": "媒体观点", "sentiment": "舆情分析", "review": "交叉核验", "host": "主持协调"}
BUSY = {"planning", "running", "reporting", "collecting_images", "analyzing_images"}


def strings(value, limit=12):
    if not isinstance(value, list):
        return []
    return [str(v)[:2000] for v in value[:limit] if isinstance(v, str) and v.strip()]


def error_text(exc):
    # Provider adapters expose sanitized messages; never persist arbitrary library errors.
    if isinstance(exc, (ValueError, RuntimeError)):
        return str(exc)[:350]
    return f"任务失败（{type(exc).__name__}），可手动重试"


class DiscussionService:
    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider or LiveProviders()
        self.crawler = CrawlerBridge(store.root)
        self.collectors = {}
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="discussion")

    def create(self, data):
        title = str(data.get("title", "")).strip()[:200]
        mode = data.get("mode", "live")
        if mode not in ("demo", "live"):
            raise ValueError("运行方式无效")
        if not title:
            raise ValueError("请填写事件名称")
        report_type = data.get("report_type", "brief")
        if report_type not in ("brief", "opinion"):
            raise ValueError("报告类型无效")
        state = {"id": uid("meeting"), "title": demo.TITLE if mode == "demo" else title,
                 "mode": mode, "requirements": str(data.get("requirements", ""))[:5000],
                 "time_range": str(data.get("time_range", ""))[:200], "report_type": report_type,
                 "web_enabled": bool(data.get("web_enabled", True)), "status": "ready",
                 "crawl_platforms": platforms(data.get("crawl_platforms", [])) if mode == "live" else [],
                 "agenda": ["核查事件经过与关键事实", "比较不同来源观点", "说明证据缺口与数据局限"],
                 "roles": ["facts", "media", "sentiment"] if mode == "demo" else ["facts", "media"],
                 "schema_version": 2, "debate_exchanges": 2,
                 "collection_mode": "adaptive", "sentiment_sample_size": 500,
                 "created_at": now(), "updated_at": now(), "operation": None,
                 "rounds": [], "messages": [], "materials": [], "comments": [],
                 "evidence": demo.evidence() if mode == "demo" else [], "snapshots": [], "reports": [], "error": ""}
        self.collection_settings(state, data)
        state['report_depth'] = self.report_depth(data.get('report_depth', 'standard'))
        self.store.create(state)
        if mode == "live":
            return self.plan(state["id"])
        return state

    def update_agenda(self, mid, data):
        agenda = strings(data.get("agenda"))
        roles = [r for r in data.get("roles", []) if r in ("facts", "media", "sentiment")]
        if not agenda or not roles:
            raise ValueError("至少保留一个议题和一个分析角色")
        def update(s):
            if s["status"] in BUSY:
                raise Conflict("任务进行中，待本轮结束后调整议程")
            s["agenda"], s["roles"] = agenda, list(dict.fromkeys(roles))
            exchanges = data.get("debate_exchanges", s.get("debate_exchanges", 2))
            if type(exchanges) is not int or exchanges not in (1, 2):
                raise ValueError("每轮交锋上限可选择1或2次")
            s["debate_exchanges"] = exchanges
            s['report_depth'] = self.report_depth(data.get('report_depth', s.get('report_depth', 'standard')))
        return self.store.change(mid, update, "agenda_updated")

    @staticmethod
    def report_depth(value):
        if value not in ('standard', 'full'):
            raise ValueError('报告篇幅无效')
        return value

    def update_sources(self, mid, data):
        selected = platforms(data.get("crawl_platforms", []))
        def update(s):
            if s["mode"] != "live":
                raise ValueError("演示模式不调用真实爬虫")
            if s["status"] in BUSY:
                raise Conflict("待本轮完成后调整采集平台")
            s["crawl_platforms"] = selected
            self.collection_settings(s, data)
        return self.store.change(mid, update, "sources_updated")

    def _claim(self, mid, status, fn=None):
        operation = uid("op")
        def claim(s):
            if s["status"] in BUSY:
                raise Conflict("已有任务正在执行，请勿重复启动")
            if fn:
                fn(s)
            s.update(status=status, operation=operation, error="")
        return self.store.change(mid, claim, "status"), operation

    def collect_images(self, mid):
        """Recover already collected post attachments; no search or LLM calls."""
        def begin(s):
            if s['mode'] != 'live':
                raise ValueError('演示会议不下载真实平台图片')
            s['image_collection'] = dict(status='running', previous_status=s['status'],
                total=0, processed=0, downloaded=0, failed=0, stopped=0,
                message='正在读取已保存的微博、小红书原始记录', stop_requested=False, started_at=now())
        state, op = self._claim(mid, 'collecting_images', begin)
        self.pool.submit(self._run_images, state, op)
        return state

    def stop_images(self, mid):
        def stop(s):
            if s['status'] != 'collecting_images':
                raise Conflict('当前没有图片补取任务')
            s['image_collection'].update(stop_requested=True, message='正在停止，已下载图片保留')
        return self.store.change(mid, stop, 'image_collection')

    def analyze_images(self, mid, data):
        ids = data.get('evidence_ids')
        if ids is not None and (not isinstance(ids, list) or any(not isinstance(x,str) for x in ids)):
            raise ValueError('请选择需要识别的帖子证据')
        def begin(s):
            if s['mode'] != 'live':
                raise ValueError('演示模式不识别真实图片')
            parents = {e['id'] for e in s['evidence'] if e.get('source_type')=='social_post' and e.get('images')}
            if ids is not None and (not ids or not set(ids) <= parents):
                raise ValueError('图片必须属于本会议已保存的帖子')
            s['image_analysis'] = dict(status='running', previous_status=s['status'],
                stop_requested=False, processed=0, total=0, completed=0, failed=0,
                message='准备识别已保存的图片', started_at=now())
        state, op = self._claim(mid, 'analyzing_images', begin)
        self.pool.submit(self._run_image_analysis, state, op, ids)
        return state

    def stop_image_analysis(self, mid):
        def stop(s):
            if s.get('image_analysis',{}).get('status') != 'running':
                raise Conflict('当前没有图片识别任务')
            s['image_analysis'].update(stop_requested=True, message='正在停止；当前请求结束后保留已识别内容')
        return self.store.change(mid, stop, 'image_analysis')

    def _run_image_analysis(self, state, op, ids):
        try:
            self._analyze_image_evidence(state, op, state['evidence'], [], ids, standalone=True)
        except Exception as exc:
            self._change(state['id'], op, lambda s:s['image_analysis'].update(status='failed', message=error_text(exc)), 'image_analysis')
        finally:
            def finish(s):
                info = s['image_analysis']
                s.update(status=info.get('previous_status','waiting'), operation=None)
            self._change(state['id'], op, finish, 'image_analysis')

    def _analyze_image_evidence(self, state, op, evidence, warnings, ids=None, standalone=False):
        from .vision import ImageAnalyzer, derived_evidence
        if state['mode'] != 'live' or not hasattr(self.provider, 'analyze_image'):
            if standalone:
                raise ValueError('当前模型适配器未提供图片识别能力')
            return []
        mid = state['id']
        existing_run = self.store.get(mid, summary=True).get('image_analysis', {})
        if existing_run.get('operation') == op and existing_run.get('stop_requested'):
            return []
        def stopped():
            s = self.store.get(mid, summary=True)
            info = s.get('image_analysis', {})
            return (s.get('operation') != op or (info.get('operation') == op and info.get('stop_requested',False))
                    or (not standalone and s['rounds'][-1].get('stop_requested',False)))
        analyzer = ImageAnalyzer(self.store.root, mid, self.provider, stopped)
        known = {e['id'] for e in evidence}
        jobs = [(e, image) for e in evidence if e.get('source_type')=='social_post' and (ids is None or e['id'] in ids)
                for image in e.get('images',[]) if image.get('status')=='downloaded'
                and not (image.get('analysis_status')=='failed' and image.get('analysis_operation')==op)
                and not (image.get('analysis_status')=='complete' and image.get('analysis_key')==analyzer.key(image)
                         and image.get('analysis_evidence_ids') and set(image['analysis_evidence_ids']) <= known)]
        if not jobs:
            if standalone:
                self._change(mid, op, lambda s:s['image_analysis'].update(status='complete', message='没有待识别的已保存图片'), 'image_analysis')
            return []
        def start(s):
            previous = s.get('image_analysis',{})
            s['image_analysis'] = dict(status='running', total=len(jobs), processed=0, completed=0, failed=0,
                operation=op, stop_requested=previous.get('stop_requested',False) if standalone else False,
                started_at=now(), message='正在提取图片文字与可见信息',
                previous_status=previous.get('previous_status','waiting'))
            if not standalone:
                s['rounds'][-1]['stage'] = '识别相关帖子配图，准备图片证据'
        self._change(mid, op, start, 'image_analysis')
        added = []
        for parent, image in jobs:
            if stopped():
                break
            record = copy.deepcopy(image)
            derived = []
            try:
                result = analyzer.run(image)
                record.update(analysis_status='complete', analysis_model=result['model'],
                    analysis_key=result['key'], analyzed_at=result['analyzed_at'], analysis_error='',
                    analysis_regions=len(result['regions']), analysis_scope=result['scope'], analysis_operation=op)
                derived = derived_evidence(parent, record, result)
                record['analysis_evidence_ids'] = [e['id'] for e in derived]
                for e in derived:
                    duplicate = next((old for old in evidence if old.get('source_type')=='image_analysis'
                        and old.get('image_sha256')==e['image_sha256'] and old.get('image_region')==e['image_region']
                        and old['text']==e['text'] and old['id']!=e['id']), None)
                    if duplicate:
                        e['duplicate_of'] = duplicate.get('duplicate_of') or duplicate['id']
            except InterruptedError:
                break
            except Exception as exc:
                record.update(analysis_status='failed', analysis_error=error_text(exc), analysis_operation=op)
                warnings.append('图片识别未完成：'+record['analysis_error'])
            def persist(s):
                target = next(e for e in s['evidence'] if e['id']==parent['id'])
                target['images'] = [copy.deepcopy(record) if i['id']==record['id'] else i for i in target['images']]
                known = {e['id'] for e in s['evidence']}
                s['evidence'].extend(copy.deepcopy(e) for e in derived if e['id'] not in known)
                info = s['image_analysis']
                info['processed'] += 1
                info['completed' if derived else 'failed'] += 1
                info['message'] = f"图片识别 {info['processed']} / {info['total']} 张；成功 {info['completed']}，失败 {info['failed']}"
            self._change(mid, op, persist, 'image_analysis')
            image.clear(); image.update(record)
            known = {e['id'] for e in evidence}
            fresh = [e for e in derived if e['id'] not in known]
            evidence.extend(fresh); added.extend(fresh)
            # A provider-wide permission/configuration failure must not be repeated per image.
            if record['analysis_status']=='failed' and any(x in record['analysis_error'] for x in ('HTTP 401','HTTP 403','视觉接口拒绝','密钥未配置')):
                break
        def complete(s):
            info = s['image_analysis']
            status = 'stopped' if stopped_local[0] else 'partial' if info['failed'] or info['processed']<info['total'] else 'complete'
            info.update(status=status, ended_at=now(), message=info['message']+
                ('；成功结果已纳入证据库' if info['completed'] else '；未产生图片证据')+
                ('；已停止，可继续未完成图片' if stopped_local[0] else ''))
        stopped_local = [stopped()]
        self._change(mid, op, complete, 'image_analysis')
        return added

    def _run_images(self, state, op):
        from .images import ImageCollector, saved_candidates
        mid = state['id']
        def stopped():
            current = self.store.get(mid, summary=True)
            return current.get('operation') != op or current.get('image_collection', {}).get('stop_requested', False)
        try:
            choices = saved_candidates(self.store.root, mid, state['evidence'])
            total = sum(len(c) for c in choices.values())
            self._change(mid, op, lambda s: s['image_collection'].update(total=total,
                message='正在下载已有帖子的配图，不调用模型' if total else '历史接口记录中没有找到可补取的配图地址'), 'image_collection')
            def download(e):
                if e.get('source_id') not in choices or stopped():
                    return
                post = dict(image_candidates=choices[e['source_id']])
                def progress(post, image):
                    def save(s):
                        target = next(item for item in s['evidence'] if item['id'] == e['id'])
                        existing = {item['id']: item for item in target.get('images', [])}
                        for item in post['images']:
                            if existing.get(item['id'], {}).get('status') != 'downloaded' or item['status'] == 'downloaded':
                                from .images import merge_image
                                existing[item['id']] = merge_image(existing.get(item['id'], {}), copy.deepcopy(item))
                        target['images'] = sorted(existing.values(), key=lambda item: item['index'])
                        status = s['image_collection']
                        status['processed'] += 1
                        key = 'downloaded' if image['status'] == 'downloaded' else 'stopped' if image['status'] == 'stopped' else 'failed'
                        status[key] += 1
                        status['message'] = f"已处理 {status['processed']} / {status['total']} 张；已保存 {status['downloaded']} 张"
                    self._change(mid, op, save, 'image_collection')
                ImageCollector(self.store.root, mid, stopped).collect_posts([post], e['platform'], retry=True, progress=progress)
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix='post-images') as workers:
                list(workers.map(download, state['evidence']))
            def finish(s):
                info = s['image_collection']
                info.update(status='stopped' if info['stop_requested'] else 'complete', ended_at=now())
                if info['stop_requested']:
                    info['message'] = f"已停止；保留 {info['downloaded']} 张图片，可再次补取"
                elif total:
                    info['message'] = f"配图补取结束：已保存 {info['downloaded']} / {total} 张，失败 {info['failed']} 张；可识别图片，或在下轮按议题自动识别"
                s.update(status=info['previous_status'], operation=None)
            self._change(mid, op, finish, 'image_collection')
        except Exception as exc:
            def fail(s):
                info = s['image_collection']
                info.update(status='failed', message=f'图片补取中断（{type(exc).__name__}），已保存文件保留', ended_at=now())
                s.update(status=info['previous_status'], operation=None)
            self._change(mid, op, fail, 'image_collection')

    def _change(self, mid, op, fn, kind="progress", payload=None):
        def guarded(s):
            if s["operation"] != op:
                raise Conflict("任务已失效")
            fn(s)
        return self.store.change(mid, guarded, kind, payload)

    def _failure(self, mid, op, exc):
        def failed(s):
            s.update(status="error", error=error_text(exc), operation=None)
            for r in s["rounds"]:
                if r["status"] == "running":
                    r["status"] = "failed"
                    for task in r["tasks"]:
                        if task["status"] in ("running", "pending"):
                            task["status"] = "failed"
                    for collection in r.get("collections", []):
                        if collection["status"] == "running":
                            collection.update(status="failed", message="本轮任务中断，可在下一轮重试采集")
            for m in s["messages"]:
                if m["status"] == "assigned":
                    m["status"] = "pending"
            for report in s["reports"]:
                if report["status"] == "running":
                    report.update(status="failed", error=error_text(exc))
        try:
            self._change(mid, op, failed, "failed")
        except Conflict:
            pass

    def plan(self, mid):
        if self.store.get(mid)["mode"] == "demo":
            raise ValueError("演示会议使用固定议程，不调用模型规划")
        state, op = self._claim(mid, "planning")
        def run():
            try:
                result = self.provider.json("host", '拟定简短议程。返回 {"agenda":["问题"],"roles":["facts","media","sentiment"]}。只选需要的角色；无评论时可不选sentiment。',
                                            {"title": state["title"], "requirements": state["requirements"],
                                             "has_comments": bool(state["comments"]), "time_range": state["time_range"]})
                agenda = strings(result.get("agenda"))
                roles = [r for r in result.get("roles", []) if r in ("facts", "media", "sentiment")]
                if not agenda or not roles:
                    raise RuntimeError("主持人议程格式不完整，请重试规划")
                self._change(mid, op, lambda s: s.update(agenda=agenda, roles=roles, status="ready", operation=None), "planned")
            except Exception as exc:
                self._failure(mid, op, exc)
        self.pool.submit(run)
        return state

    def message(self, mid, data):
        text = str(data.get("content", "")).strip()
        target = data.get("target", "all")
        if not text or len(text) > 5000:
            raise ValueError("发言长度应为 1—5000 字")
        if target not in ("all", "facts", "media", "sentiment", "review", "host"):
            raise ValueError("接收角色无效")
        def add(s):
            claim_id = data.get('claim_id')
            claim_context = None
            resolved_target = target
            if claim_id:
                source = next(((c, claim) for r in s['rounds'] for c in r['cards'] for claim in c.get('claims', []) if claim['id'] == claim_id), None)
                if source is None:
                    raise ValueError('追问主张不属于当前会议')
                card, claim = source
                claim_context = {'claim_id': claim_id, 'proposal_id': card['id'], 'agent_id': card['agent_id'], 'text': claim['text'], 'version': claim['version']}
                if target == 'all':
                    resolved_target = card['role']
            scripted = s["mode"] == "demo" and text == demo.FOLLOWUP
            s["messages"].append({"id": uid("message"), "author_id": "local-user", "content": text,
                                  "target": resolved_target, "claim_context": claim_context, "created_at": now(), "round": None,
                                  "status": "recorded" if s["mode"] == "demo" and not scripted else "pending",
                                  "response": "演示模式仅记录自由发言；可使用预设追问体验任务分派。" if s["mode"] == "demo" and not scripted else "下一轮处理"})
        return self.store.change(mid, add, "message")

    def upload(self, mid, filename, raw):
        material, evidence, comments = parse_upload(filename, raw)
        def add(s):
            if s["mode"] == "demo":
                raise ValueError("演示会议使用固定虚构材料；上传资料请创建真实分析会议")
            if any(m["id"] == material["id"] for m in s["materials"]):
                raise Conflict("这份资料已上传")
            s["materials"].append(dict(material, created_at=now()))
            s["evidence"].extend(evidence)
            s["comments"].extend(comments)
            if comments and "sentiment" not in s["roles"]:
                s["roles"].append("sentiment")
        state = self.store.change(mid, add, "material_added")
        folder = self.store.root / mid / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (material["id"] + Path(material["name"]).suffix.lower())).write_bytes(raw)
        return state

    def start_round(self, mid, data):
        def prepare(s):
            if data.get("expected_round") != len(s["rounds"]):
                raise Conflict("会议已更新，请刷新后再开始下一轮")
            if not s["agenda"]:
                raise ValueError("请先确认议程")
            resume_ids = data.get("resume_task_ids", [])
            allowed_tasks = {t["id"]: t for t in self.store.collection_tasks(mid)}
            if not isinstance(resume_ids, list) or any(i not in allowed_tasks or allowed_tasks[i]["platform"] not in s.get("crawl_platforms", []) for i in resume_ids):
                raise ValueError("续采任务无效或平台未启用")
            n = len(s["rounds"]) + 1
            for m in s["messages"]:
                if m["status"] == "pending":
                    m.update(status="assigned", round=n)
            s["rounds"].append({"number": n, "status": "running", "created_at": now(), "stop_requested": False, "collection_stop_requested": False,
                                "tasks": [], "cards": [], "warnings": [], "summary": None,
                                "stage": "主持人正在分配任务", "sentiment": None, "resume_task_ids": data.get("resume_task_ids", [])})
        state, op = self._claim(mid, "running", prepare)
        # Deep copy returned SQLite state: later uploads/messages cannot alter this round.
        checkpoints.write(self.round_folder(state) / 'input.json', state)
        self.pool.submit(self._run_round, state, op)
        return state

    def round_folder(self, state):
        return self.store.root / state['id'] / ('round-' + str(len(state['rounds'])))

    def remediate_round(self, mid, data):
        from .remediation import start
        return start(self, mid, data)

    def resume_round(self, mid):
        current = self.store.get(mid)
        if not current['rounds'] or current['rounds'][-1]['status'] not in ('failed', 'interrupted', 'cancelled'):
            raise Conflict('仅未冻结的失败或中断轮次可恢复')
        n = len(current['rounds'])
        if any(x['round'] == n for x in current['snapshots']):
            raise Conflict('已冻结轮次不可修改，请开始下一轮')
        frozen = checkpoints.read(self.round_folder(current) / 'input.json')
        if not frozen:
            raise Conflict('历史轮次没有恢复检查点，请开始下一轮')
        def prepare(s):
            r = s['rounds'][-1]
            r.update(status='running', stop_requested=False, exchange_stop_requested=False,
                     stage='正在恢复未完成阶段', stop_reason='')
            for m in s['messages']:
                if m.get('round') == n and m['status'] == 'pending':
                    m['status'] = 'assigned'
        state, op = self._claim(mid, 'running', prepare)
        frozen['operation'] = op
        if frozen['rounds'][-1].get('remediation'):
            from .remediation import run
            self.pool.submit(run, self, frozen, op)
        else:
            self.pool.submit(self._run_round, frozen, op)
        return state

    def stop_round(self, mid, action='finish_exchange'):
        if action not in ('finish_exchange', 'terminate'):
            raise ValueError('停止操作无效')
        def stop(s):
            if s['status'] != 'running':
                raise Conflict('当前没有正在运行的研讨轮次')
            r = s['rounds'][-1]
            r['stop_requested' if action == 'terminate' else 'exchange_stop_requested'] = True
            r['stop_reason'] = action
            r.setdefault('controls', []).append(dict(action=action, actor='local-user', at=now()))
        return self.store.change(mid, stop, 'stop_requested', {'action': action, 'actor': 'local-user'})

    def _search(self, query, state, warnings, question=None, condition=None):
        collector = self.collectors.get(state.get("operation"))
        return collector.search(query, question=question, condition=condition) if collector else self._search_unbudgeted(query, state, warnings)

    def _save_web_raw(self, state, query, found):
        folder = self.store.root / state['id'] / 'search_pages'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (uid('search')+'.json')).write_text(json.dumps(dict(query=query, evidence=found, collected_at=now()), ensure_ascii=False), encoding='utf-8')

    def _search_unbudgeted(self, query, state, warnings, diagnostics=None):
        diagnostics = diagnostics if diagnostics is not None else {}
        found = self.provider.search(query, state['time_range'])
        diagnostics.update(raw_count=len(found), excluded=0)
        self._save_web_raw(state, query, found)
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', state['time_range'])
        if len(dates) == 2 and dates[0] <= dates[1]:
            filtered = []
            for e in found:
                date = re.search(r'\d{4}-\d{2}-\d{2}', e.get('date', ''))
                if date and not dates[0] <= date[0] <= dates[1]:
                    warnings.append('检索结果超出指定日期范围，未入库：' + e['title'][:100])
                    continue
                e['date_scope'] = '来源日期处于指定范围；内容覆盖期仍需核验' if date else '来源日期未知，未确认满足时间范围'
                filtered.append(e)
            diagnostics['excluded'] += len(found)-len(filtered)
            found = filtered
        if not found:
            return []
        collector = self.collectors.get(state.get('operation'))
        if collector and collector.stopped():
            warnings.append('联网返回资料尚未筛选：用户已停止采集')
            self._save_web_raw(state, query, found)
            diagnostics['pending_relevance'] = True
            return []
        accepted, rejected = [], 0
        for start in range(0, len(found), 8):
            batch = found[start:start+8]
            try:
                if collector and collector.stopped():
                    raise RuntimeError('collection_stopped')
                selected = self.provider.json('review',
                    '筛选检索结果与事件主体和问题的相关性。返回 {"accepted_ids":["证据ID"]}。'
                    '不同人物同名、泛泛行业文章、无关新闻应排除；时间范围不能从当前年份推断，未知日期不等于已满足时间限制。',
                    {'title': state['title'], 'query': query, 'time_range': state['time_range'], 'evidence': batch, '_request_timeout': 60})
                ids = selected.get('accepted_ids')
                valid = {e['id'] for e in batch}
                if not isinstance(ids, list) or any(not isinstance(x, str) or x not in valid for x in ids):
                    raise ValueError('invalid_relevance_ids')
                accepted.extend(ids)
                rejected += len(batch)-len(set(ids))
            except Exception as exc:
                diagnostics.update(pending_relevance=True, error_type=type(exc).__name__)
                warnings.append('部分网页相关性筛选未完成，已保存原始结果并保留此前通过的材料')
                break
        diagnostics['excluded'] += rejected
        if rejected:
            warnings.append(f'检索“{query}”：{rejected}条结果未通过事件相关性筛选，未入库')
        found = [e for e in found if e['id'] in accepted]
        if collector and hasattr(self.provider, 'fetch_page'):
            folder = self.store.root / state['id'] / 'search_pages'
            for source in list(found):
                if collector.stopped():
                    break
                if source['url'] in collector.fetched_urls:
                    continue
                collector.fetched_urls.add(source['url'])
                def requested():
                    with collector._state_lock:
                        collector.request_count += 1
                pages, record, body = self.provider.fetch_page(source, 20, collector.stopped, requested)
                collector.web_fetches.append(record)
                (folder / (uid('page')+'.json')).write_text(json.dumps(dict(record=record,text=body),ensure_ascii=False),encoding='utf-8')
                found.extend(pages)
                if record['status']!='fetched':
                    warnings.append('网页正文未取得，保留搜索摘要：'+source['title'][:70]+'（'+record['status']+('，HTTP '+str(record['http_status']) if record.get('http_status') else '')+'）')
        return found

    def _tasks(self, state, feedback):
        n = len(state["rounds"])
        previous = state["snapshots"][-1] if state["snapshots"] else None
        if state["mode"] == "demo":
            roles = ["facts", "sentiment"] if n > 1 else state["roles"]
            return [configure_collaboration({"role": role, "question": demo.FOLLOWUP if n > 1 else "；".join(state["agenda"]),
                     "feedback_ids": [m["id"] for m in feedback]}) for role in roles]
        result = self.provider.json("host", '将议程或追问分配给必要角色。只调用相关角色，role限facts/media/sentiment。返回 '
            '{"tasks":[{"role":"facts","question":"具体任务","analysis_kind":"fact_check","debate":true,"reason":"采用该协作方式的具体原因",'
            '"search_query":"简短联网检索关键词","retrieval_query":"资料检索关键词","needs_comments":false,"feedback_ids":["消息ID"]}]}。'
            '最多3个功能任务，每角色一个；每个任务必须给出analysis_kind、debate、reason，不按任务顺序确定谁能辩论。'
            'analysis_kind限fact_check（核查关键事实）、viewpoint_comparison（比较或解释媒体观点）、sentiment_interpretation（解释情绪、诉求或样本偏差）、'
            'aggregation（仅列出程序统计）、source_digest（仅整理来源与摘要）。前三类必须debate:true，分别独立回答后互审；'
            '媒体观点media、舆情分析sentiment固定debate:true，均由两个独立实例分析并互相质询，不能因source_digest或aggregation降为单实例。'
            '事实角色的来源整理与统计汇总可debate:false。不得预设正反立场或强迫分歧；认可时仍可核查对方主张的依据与适用范围。'
            '舆情实例共用程序统计，只讨论解释和适用范围；'
            'roles是此前参与的角色，不是新增任务的限制。用户要求了解网友反应、评论样本、情绪或诉求时，应在采集前安排sentiment任务、needs_comments:true；不能因为has_comments=false就省略任务。'
            '其他角色需要评论原文时也设置needs_comments:true，并在question写明采集目的和范围。仅要求事实或报道时不额外采评论。'
            '没有评论但有平台帖子时可以进行定性观点比较；完全没有用户表达材料时说明缺口，不虚构情绪。'
            '必须处理所有反馈，尊重用户指定角色；host/review接收的指导交由相关角色落实。检索词应包含事件与关注点，不包含报告写作要求。'
            '资料含英文时retrieval_query必须同时包含相关英文关键词，不能仅用中文检索英文资料。',
                                    {"agenda": state["agenda"], "roles": state["roles"], "feedback": feedback,
                                     "previous_summary": previous["summary"] if previous else None,
                                     "has_comments": bool(state["comments"]), "title": state["title"], "requirements": state["requirements"],
                                     "has_platform_posts": any(e.get('source_type') == 'social_post' for e in state['evidence']),
                                     "materials": [{"name": m["name"], "preview": next((e["text"][:300] for e in state["evidence"] if e["source_id"] == m["id"]), "")} for m in state["materials"]]})
        allowed_ids = {m["id"] for m in feedback}
        allowed_roles = {"facts", "media", "sentiment"}
        tasks = []
        for t in result.get("tasks", [])[:3]:
            if isinstance(t, dict) and t.get("role") in allowed_roles and t.get("question"):
                if any(x["role"] == t["role"] for x in tasks):
                    continue
                tasks.append({"role": t["role"], "question": str(t["question"])[:2000],
                              "search_query": str(t.get("search_query") or state["title"])[:200],
                              "retrieval_query": str(t.get("retrieval_query") or t["question"])[:800],
                              "debate": t.get("debate") is True,
                              "analysis_kind": t.get("analysis_kind"),
                              "needs_comments": t.get("needs_comments") is True or t["role"] == "sentiment",
                              "reason": str(t.get("reason", ""))[:400],
                              "feedback_ids": [i for i in t.get("feedback_ids", []) if i in allowed_ids]})
        if not tasks:
            raise RuntimeError("协调器未生成有效任务，请重试")
        # Every message is explicitly routed, including ones omitted by the coordinator.
        for m in feedback:
            targeted = m["target"] if m["target"] in ("facts", "media", "sentiment") else None
            assigned = next((t for t in tasks if t["role"] == targeted), None) if targeted else next((t for t in tasks if m["id"] in t["feedback_ids"]), None)
            if assigned is None and targeted:
                assigned = {"role": targeted, "question": m["content"], "feedback_ids": []}
                tasks.append(assigned)
            assigned = assigned or tasks[0]
            if m["id"] not in assigned["feedback_ids"]:
                assigned["feedback_ids"].append(m["id"])
                assigned["question"] += "\n用户指导：" + m["content"]
        return [configure_collaboration(t) for t in tasks]

    def _card(self, state, task, evidence, sentiment, previous, capture=None):
        role = task["role"]
        n = len(state["rounds"])
        if state["mode"] == "demo":
            result = demo.debate_card(role, task, evidence, n, previous)
        elif role == "sentiment" and not task.get('require_reciprocal') and not state["comments"] and not any(e.get('source_type')=='social_post' for e in evidence):
            result = {"conclusion": "当前没有可分析的评论样本，无法给出情感比例。", "evidence_ids": [],
                      "limitations": ["媒体报道不能代替评论样本"], "changes": "未生成情感统计", "response": "请上传含text列的评论CSV。"}
        else:
            verifying = task.get('phase') == 'verification'
            selected = evidence_context(evidence, previous, task.get("evidence_ids"),
                                        state["title"] + " " + task["question"] + " " + task.get("retrieval_query", ""), top_k=2 if verifying else 10)
            if role == 'sentiment' and not verifying and sentiment:
                # BM25 alone favors long posts over short comments. Add bounded,
                # reproducible qualitative examples from the actual classified sample.
                from collections import defaultdict, deque
                labels = {x['id']: x['label'] for x in sentiment.get('labels', [])}
                groups = defaultdict(list)
                for comment in state['comments']:
                    if comment['id'] in labels:
                        groups[(comment.get('source', ''), labels[comment['id']])].append(comment)
                queues = deque(deque(sorted(v, key=lambda c: c['id'])) for _, v in sorted(groups.items()))
                by_id = {e['id']: e for e in evidence}
                selected_ids = {e['id'] for e in selected}
                size = sum(len(e['text']) for e in selected)
                for _ in range(18):
                    if not queues:
                        break
                    queue = queues.popleft()
                    example = by_id.get(queue.popleft().get('evidence_id'))
                    if queue:
                        queues.append(queue)
                    if example and example['id'] not in selected_ids and size + len(example['text']) <= 48000:
                        selected.append(example)
                        selected_ids.add(example['id'])
                        size += len(example['text'])
            instruction = (
                '完成指定角色的任务。对BM25候选片段按相关性筛选，仅引用确实用于结论的证据。返回 '
                '{"conclusion":"简明分析结论，事实句用[E-编号]引用", "evidence_ids":["E-编号"],'
                '"limitations":["局限"],"response":"回应其他观点或用户要求", "changes":"相较上一轮新增、修订或维持的结论及依据"}。'
                '同时返回claims:[{text,kind,scope,evidence_ids,opposing_evidence_ids,action}]，最多3条；kind为fact/opinion/statistic/inference，action为maintain/qualify/revise/withdraw。'
                '修订时每条claim还应返回previous_id，对应原主张ID；新主张不填。'
                '不确定时保留缺口，比例只能使用给定sentiment统计。帖子观点可作定性分析，但帖子不是评论；没有评论样本时严禁生成评论情感比例。'
                '检索片段只是上下文中的例证子集，不能把展示的片段数量误称为采集总数或分类样本数。'
                '严格区分原始发布与媒体转述：转述官方通报的网页不等于已获得通报原文；评论者的定性不得当成官方定性。' + task.get("instruction", ""))
            if verifying:
                instruction = '输出简洁JSON，包含conclusion、evidence_ids、verifications、replies；不重写报告、不生成新主张。' + task.get('instruction', '')
            repairing = task.get('phase') == 'revision' and previous.get('reply_repair')
            if repairing:
                instruction = (
                    '上次修订正文已保存，仅补齐reply_repair指出的质询回应。返回JSON {"replies":[{challenge_id,action,response,evidence_ids}]}。'
                    '只回答required_challenge_ids，每个ID恰好一项，不重写conclusion、claims或已经有效的回复。'
                    'action只能为maintain/qualify/revise/withdraw；response不能为空，说明依据与如何满足解决条件。'
                    '依据revision_draft、peer_reviews的原始问题及提供证据，不能编造或改变已保存正文。若正文无法支持所需回应，应明确说明冲突，等待核验。')
            request_data = {"role": ROLES[role], "task": task, "evidence": selected, "sentiment": sentiment,
                 "previous": previous, "requirements": state["requirements"],
                 "sample_scope": {"comment_count": len(state['comments']), "post_count": sum(e.get('source_type')=='social_post' for e in evidence)},
                 "_request_timeout": 150 if verifying else 100, "_max_tokens": 2200 if verifying else 4500,
                 "user_feedback": [m for m in state["messages"] if m["id"] in task["feedback_ids"]]}
            if task.get('phase') == 'revision':
                instruction += 'reply_requirements是本次必须回应的精确清单；只复制该清单中的challenge_id，每项恰好一次。不要把claim_id、任务编号或上次已答问题的编号当作challenge_id。'
                request_data['reply_requirements'] = [dict(challenge_id=c['id'], question=c['question'], resolution=c.get('resolution', ''))
                    for review in previous['peer_reviews'] for c in review['challenges'] if c['target_agent'] == task['agent_id']]
            if repairing:
                from .reply_repair import repair_replies
                result = repair_replies(self, state, task, instruction, request_data, capture if capture is not None else {})
            else:
                if capture is not None:
                    capture['requests'] = 1
                result = self.provider.json(role, instruction, request_data)
            if capture is not None:
                capture['model_output'] = copy.deepcopy(result)
            result['_input_chars'] = len(instruction) + len(json.dumps(request_data, ensure_ascii=False))
            allowed = {e["id"] for e in selected}
            if not isinstance(result.get("evidence_ids", []), list) or any(x not in allowed for x in result.get("evidence_ids", [])):
                raise RuntimeError("智能体引用了未提供的证据，结果未采纳，请重试")
            mentioned = set(re.findall(r"E-[a-f0-9]{12}", json.dumps(result, ensure_ascii=False)))
            if not mentioned <= allowed:
                raise RuntimeError("智能体正文包含未知引用，结果未采纳")
        if not isinstance(result.get("conclusion"), str) or not result["conclusion"].strip():
            raise RuntimeError("智能体未返回分析结论")
        refs = set(re.findall(r"E-[a-f0-9]{12}", json.dumps(result, ensure_ascii=False)))
        return {"role": role, "role_name": ROLES[role], "task_id": task["id"], "round": n, "input_chars": result.get('_input_chars', 0),
                "task": task["question"], "feedback_ids": task["feedback_ids"], "conclusion": result["conclusion"][:7000],
                "evidence_ids": sorted(refs | set(strings(result.get("evidence_ids"), 30))), "limitations": strings(result.get("limitations")),
                "claims": result.get("claims", []), "review_data": result.get("review", {}),
                "verification_data": result.get("verifications", []), "reply_data": result.get("replies", []),
                "response": str(result.get("response", ""))[:3000], "changes": str(result.get("changes", ""))[:3000],
                "challenge_role": result.get("challenge_role") if result.get("challenge_role") in ("facts", "media", "sentiment") else None,
                "challenge_question": str(result.get("challenge_question", ""))[:2000]}

    @staticmethod
    def collection_settings(state, data):
        state['collection_mode'] = 'adaptive'
        for key, default in [('sentiment_sample_size', 500)]:
            value = data.get(key, state.get(key, default))
            if type(value) is not int or value <= 0 or value > 2147483647:
                raise ValueError('抽样数量须为正整数')
            state[key] = value

    def stop_collection(self, mid):
        def stop(s):
            if s['status'] != 'running':
                raise Conflict('当前没有正在执行的轮次')
            s['rounds'][-1]['collection_stop_requested'] = True
            s['rounds'][-1].setdefault('controls', []).append(dict(action='stop_collection', actor='local-user', at=now()))
        return self.store.change(mid, stop, 'collection_stop_requested')

    def resume_collection(self, mid, data):
        state = self.store.get(mid)
        if state['status'] in BUSY:
            raise Conflict('待本轮完成后继续采集')
        ids = data.get('task_ids', [])
        tasks = self.store.collection_tasks(mid)
        if not isinstance(ids, list) or not ids or any(i not in {t['id'] for t in tasks} for i in ids):
            raise ValueError('请选择本会议的采集任务')
        if any(not t.get('resumable') for t in tasks if t['id'] in ids):
            raise ValueError('所选任务已结束或不支持游标续采，请通过新问题发起查询')
        if any(t['platform'] not in state.get('crawl_platforms', []) for t in tasks if t['id'] in ids):
            raise ValueError('请先启用任务对应的平台')
        return self.start_round(mid, dict(data, resume_task_ids=ids))

    def _sentiment(self, comments, state=None):
        def stopped():
            if not state or not state.get('operation'):
                return False
            current = self.store.get(state['id'], summary=True)
            return current.get('operation') != state['operation'] or current['rounds'][-1].get('stop_requested', False)
        return classify(self.store, self.provider, comments, (state or {}).get('sentiment_sample_size', 500), (state or {}).get('id', 'discussion'), stopped=stopped)

    def _cancel_round_if_requested(self, mid, op):
        current = self.store.get(mid, summary=True)
        if current.get('operation') != op:
            raise Conflict('任务已失效')
        if not current['rounds'][-1].get('stop_requested'):
            return False
        def cancel(s):
            r = s['rounds'][-1]
            r.update(status='cancelled', stage='本轮已终止，尚未冻结报告快照', report_ready=False)
            for task in r['tasks']:
                if task['status'] in ('pending', 'running'):
                    task.update(status='interrupted', error='用户已终止本轮', finished_at=now())
            s.update(status='interrupted', operation=None)
        self._change(mid, op, cancel, 'round_cancelled')
        return True

    def _collect_social(self, state, op, tasks, feedback, evidence, warnings):
        collector = self.collectors[op]
        if state['mode'] != 'live':
            return []
        resume_ids = state['rounds'][-1].get('resume_task_ids', [])
        queries = [t.get('search_query') or state['title'] + ' ' + t['question'][:160]
                   for t in tasks] if state['web_enabled'] else []
        records = collector.run(tasks=tasks, feedback=feedback, resume_ids=resume_ids, web_queries=queries,
                                initial=state.get('debate_exchanges', 2) > 0)
        if state['comments'] and not any(t['role']=='sentiment' for t in tasks):
            task = configure_collaboration(dict(id=uid('task'), role='sentiment', question='分析评论抽样的情感分布、诉求、来源与局限', retrieval_query=state['title']+' 评论', feedback_ids=[], status='pending', error=''))
            tasks.append(task)
            self._change(state['id'], op, lambda s: s['rounds'][-1]['tasks'].append(copy.deepcopy(task)), 'tasks')
        return records

    def _run_round(self, state, op):
        mid = state["id"]
        n = len(state["rounds"])
        feedback = [m for m in state["messages"] if m["status"] == "assigned" and m["round"] == n]
        try:
            if self._cancel_round_if_requested(mid, op):
                return
            folder = self.round_folder(state)
            prepared_input = checkpoints.read(folder / 'prepared.json')
            acquired = checkpoints.read(folder / 'acquired.json') if not prepared_input else None
            if acquired:
                state['evidence'], state['comments'] = acquired['evidence'], acquired['comments']
            planned = checkpoints.read(folder / 'plan.json')
            if planned is None:
                planned = self._tasks(state, feedback)
                for t in planned:
                    t.update(id=uid("task"), status="pending", error="")
                checkpoints.write(folder / 'plan.json', planned)
            tasks = copy.deepcopy(planned)
            def planning(s):
                known = {t['id'] for t in s['rounds'][-1]['tasks']}
                s['rounds'][-1]['tasks'].extend(copy.deepcopy(t) for t in tasks if t['id'] not in known)
                s['rounds'][-1]['stage'] = '检索与准备证据'
            self._change(mid, op, planning, 'tasks')
            evidence = copy.deepcopy(prepared_input['evidence'] if prepared_input else state['evidence'])
            warnings = list(prepared_input['warnings']) if prepared_input else []
            self.collectors[op] = RoundCollector(self, state, op, evidence, warnings)
            if acquired:
                budget = self.store.get(mid, summary=True)['rounds'][-1].get('collection_budget', {})
                self.collectors[op].used = budget.get('used_seconds', 0)
                self.collectors[op].request_count = budget.get('request_count', 0)
            if prepared_input:
                state['comments'] = prepared_input['comments']
                tasks = prepared_input['tasks']
                sentiment = prepared_input['sentiment']
                collector = self.collectors[op]
                collector.records = prepared_input['collections']
                collector.used = prepared_input['collection_used']
                collector.request_count = prepared_input['request_count']
            else:
                self._collect_social(state, op, tasks, feedback, evidence, warnings)
                if self._cancel_round_if_requested(mid, op):
                    return
                from .vision import select_posts
                image_posts = select_posts(evidence, [t.get('retrieval_query') or t.get('question','') for t in tasks])
                self._analyze_image_evidence(state, op, evidence, warnings, image_posts)
                if self._cancel_round_if_requested(mid, op):
                    return
                sentiment = None
                if state["mode"] == "live" and state["comments"] and any(t["role"] == "sentiment" for t in tasks):
                    self._change(mid, op, lambda s: s["rounds"][-1].update(stage="分析上传及平台采集评论样本"))
                    sentiment = self._sentiment(state["comments"], state)
                    if sentiment["failed"]:
                        warnings.append(f"{sentiment['failed']}条去重评论分类失败，未纳入比例分母")
                if self._cancel_round_if_requested(mid, op):
                    return
                collector = self.collectors[op]
                checkpoints.write(folder / 'prepared.json', dict(evidence=evidence, comments=state['comments'],
                    tasks=tasks, sentiment=sentiment, warnings=warnings, collections=collector.records,
                    collection_used=collector.used, request_count=collector.request_count))
            if not evidence:
                warnings.append("当前没有可用证据，仅能说明数据缺口，不能形成已核实的事实结论")
            def prepared(s):
                ids = {e["id"] for e in s["evidence"]}
                s["evidence"].extend(e for e in evidence if e["id"] not in ids)
                s["rounds"][-1].update(stage="分析智能体正在研讨", warnings=warnings, sentiment=sentiment)
            self._change(mid, op, prepared, "evidence")
            if self._cancel_round_if_requested(mid, op):
                return
            previous = state["snapshots"][-1]["cards"] if state["snapshots"] else []
            runner = DebateRunner(self, state, op, evidence, sentiment, warnings)
            cards = runner.run(tasks)
            sentiment = runner.sentiment
            if self._cancel_round_if_requested(mid, op):
                return
            # Keep each instance; only its own newer proposal supersedes its draft.
            active_roles = {t["role"] for t in tasks}
            current = {c.get("agent_id", c["role"]): c for c in previous if c["role"] not in active_roles and c["role"] != "review"}
            for c in cards:
                if c.get("phase") in ("proposal", "revision"):
                    current[c["agent_id"]] = c
            self._change(mid, op, lambda s: s["rounds"][-1].update(stage="主持人汇总，准备等待用户决策"))
            if state["mode"] == "demo":
                summary = demo.summary(n)
            else:
                summary = self.provider.json("host", '汇总研讨。返回 {"overview":"简明纪要","agreements":["共识"],"disagreements":["分歧"],"gaps":["证据或任务缺口"],"next_questions":["下一轮建议"]}。失败角色与未解决反馈必须写入缺口；不把重复观点当独立验证。仅使用最新主张及核验状态，未核验/部分支持不得升级为确定事实。检索未发现回应不等于当事人从未回应；媒体称账号注销也不能证明没有回应。评论比例仅描述已分类样本，不代表公众，帖子数量不能代替评论样本。保留转载、媒体归因与网络质疑的来源层级。',
                                             {"cards": list(current.values()), "feedback": feedback, "warnings": warnings, "issues": runner.issues, "peer_reviews": runner.reviews, "verifications": runner.verifications})
                if not isinstance(summary.get("overview"), str) or not summary["overview"].strip():
                    raise RuntimeError("主持汇总不完整，本轮尚不能生成报告，请重试")
                summary = {"overview": summary["overview"][:5000], **{k: strings(summary.get(k)) for k in ("agreements", "disagreements", "gaps", "next_questions")}}
                if not set(re.findall(r"E-[a-f0-9]{12}", json.dumps(summary))) <= {e["id"] for e in evidence}:
                    raise RuntimeError("主持汇总包含未知证据引用，本轮尚不能生成报告，请重试")
            summary["gaps"] = list(dict.fromkeys(summary["gaps"] + warnings))
            verification = checkpoints.verification_summary(list(current.values()))
            if state.get('report_depth') == 'full' and verification['pending']:
                raise RuntimeError('关键核验尚未完成，请恢复本轮未完成阶段；成功任务会复用，不生成正式快照')
            snapshot = {"verification_summary": verification, "report_ready": verification['pending'] == 0,
                        "id": uid("snapshot"), "round": n, "created_at": now(), "cards": list(current.values()),
                        "summary": summary, "evidence": evidence, "sentiment": sentiment or (state["snapshots"][-1].get("sentiment") if state["snapshots"] else None),
                        "feedback": feedback, "agenda": state["agenda"], "warnings": warnings, "collections": [public_task(t) for t in self.collectors[op].records],
                        "schema_version": 2, "issues": copy.deepcopy(runner.issues), "proposals": copy.deepcopy(cards),
                        "peer_reviews": copy.deepcopy(runner.reviews), "verifications": copy.deepcopy(runner.verifications),
                        "tool_runs": copy.deepcopy(runner.tools), "evidence_revision": cards[-1].get("evidence_revision"),
                        "claims": [copy.deepcopy(c) for p in current.values() for c in p.get("claims", [])]}
            def complete(s):
                s["rounds"][-1].update(status="complete", summary=summary, warnings=warnings, stage="等待用户决策", verification_summary=verification, report_ready=verification["pending"] == 0)
                for m in s["messages"]:
                    if m["status"] == "assigned" and m["round"] == n:
                        replies = [c for c in current.values() if m["id"] in c["feedback_ids"]]
                        m.update(status="answered" if replies else "unresolved",
                                 resolution="partial" if any(i["status"] != "reviewed" for i in runner.issues if m["id"] in i["feedback_ids"]) else "responded",
                                 response="\n".join(c["role_name"] + "：" + c["response"] for c in replies) or "本轮未完成处理，请再次追问",
                                 task_ids=[c["task_id"] for c in replies])
                snapshot["feedback"] = copy.deepcopy([m for m in s["messages"] if m.get("round") and m["round"] <= n and m["status"] in ("answered", "unresolved")])
                s["snapshots"].append(snapshot)
                s.update(status="waiting", operation=None)
            self._change(mid, op, complete, "round_complete")
        except Exception as exc:
            self._failure(mid, op, exc)
        finally:
            self.collectors.pop(op, None)

    def _task_status(self, mid, op, task_id, status, error="", error_type=""):
        def update(s):
            task = next(t for t in s["rounds"][-1]["tasks"] if t["id"] == task_id)
            task.update(status=status, error=error)
            if status == 'running':
                task['started_at'] = now()
                task['error_type'] = ''
            elif status in ('complete', 'failed'):
                task['finished_at'] = now()
                task['error_type'] = (error_type or ('timeout' if 'Timeout' in error else 'execution_error')) if error else ''
        self._change(mid, op, update, "task_status")

    def _append_card(self, mid, op, card):
        def append(s):
            cards = s["rounds"][-1]["cards"]
            cards[:] = [c for c in cards if c['id'] != card['id']]
            cards.append(card)
            next(t for t in s["rounds"][-1]["tasks"] if t["id"] == card["task_id"]).update(
                status="complete", finished_at=now(), error='', error_type='', response_validation=None,
                context_chars=card.get('input_chars', 0))
        self._change(mid, op, append, {"proposal": "proposal_submitted", "revision": "proposal_revised", "peer_review": "peer_review_posted"}.get(card.get("phase"), "card"))

    def report(self, mid, data):
        from .report_style import GENERATION_VERSION
        report_id = uid("report")
        def prepare(s):
            nonlocal report_id
            snapshot = next((x for x in s["snapshots"] if x["id"] == data.get("snapshot_id")), None)
            if not snapshot:
                raise ValueError("请选择已完成轮次的快照")
            if s.get('report_depth') == 'full':
                checks = snapshot.get('verification_summary') or checkpoints.verification_summary(snapshot['cards'])
                if checks['pending'] or checks['status'] != 'complete':
                    raise ValueError('完整报告需要先完成最新主张核验；历史快照保持不变，请在新轮次补齐核验')
            published = next((r for r in reversed(s['reports']) if r['snapshot_id']==snapshot['id'] and r['status']=='complete'), None)
            if published and not (data.get('recheck') is True and (published.get('review_version',1)<report_checks.REVIEW_VERSION
                    or published.get('generation_version',1)<GENERATION_VERSION)):
                raise Conflict("该快照已经生成报告，请打开已有版本；继续研讨可生成新版本")
            previous = next((r for r in reversed(s['reports']) if r['snapshot_id']==snapshot['id'] and r['status'] in ('failed','interrupted') and r.get('report_depth','standard')==s.get('report_depth','standard') and r.get('generation_version',1)==GENERATION_VERSION and r.get('review_version',1)==report_checks.REVIEW_VERSION), None)
            if previous:
                report_id = previous['id']
                previous.update(status='running', error='', progress='恢复报告生成与章节审查')
                return
            s["reports"].append({"id": report_id, "snapshot_id": snapshot["id"], "round": snapshot["round"],
                                 "created_at": now(), "status": "running", "progress": "等待报告引擎", "error": "", "report_depth": s.get("report_depth", "standard"),
                                 "review_version": report_checks.REVIEW_VERSION, "generation_version": GENERATION_VERSION,
                                 "supersedes": published['id'] if published else None})
        state, op = self._claim(mid, "reporting", prepare)
        snapshot = next(x for x in state["snapshots"] if x["id"] == data["snapshot_id"])
        self.pool.submit(self._run_report, state, op, report_id, snapshot)
        return state

    def _run_report(self, state, op, report_id, snapshot):
        mid = state["id"]
        folder = self.store.root / mid / report_id
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            def progress(message):
                self._change(mid, op, lambda s: next(r for r in s['reports'] if r['id']==report_id).update(progress=message[:200]), "report_progress")
            cached = checkpoints.read(folder / 'engine-output.json')
            record = next(r for r in state['reports'] if r['id']==report_id)
            if not cached and record.get('supersedes'):
                previous_folder = self.store.root / mid / record['supersedes']
                previous_report = next(r for r in state['reports'] if r['id']==record['supersedes'])
                previous_snapshot = checkpoints.read(previous_folder / 'snapshot.json')
                if (previous_snapshot and previous_report.get('report_depth','standard')==record.get('report_depth','standard')
                        and previous_report.get('generation_version',1)==record.get('generation_version',1)
                        and checkpoints.digest(previous_snapshot)==checkpoints.digest(snapshot)):
                    cached = checkpoints.read(previous_folder / 'engine-output.json')
                    if cached:
                        checkpoints.write(folder / 'engine-output.json', cached)
            if cached:
                md = cached['markdown']
            else:
                md = demo.report(state, snapshot) if state['mode']=='demo' else self.provider.report(state, snapshot, folder, progress)
                checkpoints.write(folder / 'engine-output.json', {'markdown': md})
            if state['mode']=='live' and hasattr(self.provider, 'audit_report') and snapshot.get('schema_version')==2:
                md = report_checks.review(self.provider, md, snapshot, folder, progress, state.get('report_depth')=='full')
            else:
                report_checks.validate(md, snapshot)
            if state["mode"] == "demo":
                md += "\n全部内容为虚构演示，不用于真实事件判断。\n"
            # Operational records remain in the immutable snapshot/workbench.
            # The published document contains only reviewed event analysis.
            report_checks.validate(md, snapshot)
            (folder / "report.md").write_text(md, encoding="utf-8")
            def finish(s):
                next(r for r in s['reports'] if r['id']==report_id).update(status="complete", progress="报告已完成")
                if record.get('supersedes'):
                    next(r for r in s['reports'] if r['id']==record['supersedes'])['superseded_by'] = report_id
                s.update(status="waiting", operation=None)
            self._change(mid, op, finish, "report_complete")
        except Exception as exc:
            self._failure(mid, op, exc)

    def public(self, state):
        result = copy.deepcopy(state)
        result.pop("comments", None)
        result["comment_count"] = state.get("comments_count", 0) if state.get("_summary_only") else len(state["comments"])
        result["evidence_count"] = state.get("evidence_count", 0) if state.get("_summary_only") else len(state["evidence"])
        result["evidence"] = result["evidence"][:30]
        result["evidence_paged"] = True
        result["snapshots"] = [{k: x[k] for k in ("id", "round", "created_at", "summary")} for x in state["snapshots"]]
        if result["rounds"]:
            for r in result["rounds"]:
                records = r.get('collections', [])
                from .collection import explain_supplements
                r['tool_runs'] = explain_supplements(r.get('tool_runs', []), records)
                r['collection_summary'] = dict(total=len(records), attention=sum(t.get('status') in
                    ('failed','timeout','needs_login','rate_limited','budget_exhausted','interrupted','unavailable','assessment_failed','stalled','model_stopped','content_unavailable','busy') for t in records))
                r['collections'] = records[:4]
                if r.get("sentiment"):
                    r["sentiment"].pop("labels", None)
        result["demo_followup"] = demo.FOLLOWUP
        return result
