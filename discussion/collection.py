"""Model-directed, resumable collection shared by initial research and debate tools."""
import copy
import hashlib
import json
import re
import time
from collections import Counter, deque
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import RLock, Lock

from .crawler import PLATFORMS, SCOPE
from .evidence import retrieve
from .store import now, uid
from . import checkpoints

REASONS = {
    'model_stopped': '协调模型结束本项采集，未解决缺口已保留',
    'assessment_failed': '协调评估未完成，暂停该需求，不能判定材料充分',
    'reserved_for_supplement': '初始采集阶段结束，剩余时间预留给辩论补证',
    'satisfied': '当前问题已有相应材料，仍需核验', 'exhausted': '当前查询返回结果已耗尽',
    'stalled': '连续三页无新增或游标重复', 'budget_exhausted': '本轮采集时间预算用完，资料尚未穷尽',
    'user_stopped': '用户停止采集，本轮不再自动补采', 'needs_login': '平台需要登录或验证',
    'failed': '接口或网络错误，断点已保存', 'interrupted': '服务中断，等待手动继续',
    'unavailable': '采集依赖未就绪', 'busy': '平台正在被其他任务使用',
    'cursor_invalid': '分页断点失效，已记录并等待重新查询', 'timeout': '单次请求超时或连接无进展，断点已保存',
    'rate_limited': '平台限制访问频率，暂停该平台，其他来源继续',
    'content_unavailable': '当前帖子或评论不可访问，继续其他帖子',
}


def public_task(task):
    # Private cursor and xsec metadata are intentionally excluded.
    fields = ('id', 'platform', 'platform_name', 'query', 'question', 'reason', 'condition',
              'kind', 'status', 'stop_reason', 'message', 'page', 'post_count', 'comment_count',
              'new_evidence_count', 'new_comment_count', 'duplicates', 'excluded', 'request_count',
              'assessment', 'decisions', 'search_session_id', 'scope', 'collected_at', 'round', 'resumable', 'parent_task', 'cursor_resets', 'web_pages', 'search_providers',
              'raw_count', 'returned_count', 'pending_relevance', 'item_failures', 'error_type', 'result_diagnostic',
              'comments', 'comment_question', 'comment_coverage')
    return {k: copy.deepcopy(task[k]) for k in fields if k in task}


def supplement_outcome(task, new_ids, collections=None):
    """Keep 'not executed' distinct from a completed search with no new material."""
    chain = [t for t in collections or [] if task.get('search_session_id') and t.get('search_session_id')==task['search_session_id']] or [task]
    task = dict(task, **{k: sum(t.get(k, 0) or 0 for t in chain)
                        for k in ('request_count','raw_count','returned_count','duplicates','excluded')
                        if any(k in t for t in chain)})
    task.update({k: chain[-1][k] for k in ('stop_reason','status','message') if k in chain[-1]})
    status = task.get('stop_reason') or task.get('status', 'unknown')
    count = task.get('request_count', 0)
    if status in ('budget_exhausted', 'reserved_for_supplement', 'user_stopped'):
        message = ('未发起联网补证：' if not count else '补证未完成：') + task.get('message', REASONS.get(status, status))
    elif status in ('failed', 'timeout', 'needs_login', 'rate_limited', 'unavailable', 'assessment_failed', 'content_unavailable', 'busy'):
        message = '补证未完成：' + task.get('message', REASONS.get(status, status))
        if new_ids:
            message += f'；已保存 {len(new_ids)} 条新增材料'
    elif new_ids:
        status, message = 'complete', f'新增 {len(new_ids)} 条材料，是否支持质疑由后续核验判断'
    elif task.get('duplicates'):
        status, message = 'duplicates', f"返回材料中 {task['duplicates']} 条已在证据库，无新增；可复用已有原文"
    elif task.get('excluded'):
        status, message = 'filtered', f"返回材料未通过相关性或日期筛选（排除 {task['excluded']} 条），无可入库结果"
    elif task.get('raw_count') == 0:
        status, message = 'no_results', '已执行联网检索，搜索服务未返回材料'
    else:
        status, message = 'empty', '本次检索未增加材料；历史记录未提供完整返回及筛选数量'
    return dict(status=status, message=message, collection_task_id=task.get('id'),
                collection_task_ids=[t['id'] for t in chain if t.get('id')],
                request_count=count, raw_count=task.get('raw_count'), returned_count=task.get('returned_count'),
                duplicates=task.get('duplicates', 0), excluded=task.get('excluded', 0))


def explain_supplements(records, collections):
    """Read-only correction of old ambiguous labels using their persisted request logs."""
    result = copy.deepcopy(records)
    for record in result:
        if record.get('collection_task_id') or record.get('platform') or record.get('status') != 'empty':
            continue
        matches = [t for t in collections if t.get('platform')=='web' and t.get('query')==record.get('query')]
        if len(matches) == 1:
            record.update(supplement_outcome(matches[0], record.get('new_evidence_ids', [])))
    return result


class RoundCollector:
    def __init__(self, service, state, op, evidence, warnings):
        self.service, self.store, self.state, self.op = service, service.store, state, op
        self.mid, self.number = state['id'], len(state['rounds'])
        self.evidence, self.warnings = evidence, warnings
        self.used, self.active = 0.0, None
        self.phase = 'supplement'
        self.records = []
        self.fetched_urls, self.web_fetches = set(), []
        self.request_count = 0
        self.blocked_platforms = {}
        self._state_lock = RLock()
        self._publish_lock = Lock()

    def stopped(self):
        s = self.store.get(self.mid, summary=True)
        r = s['rounds'][-1]
        return s.get('operation') != self.op or r.get('collection_stop_requested') or r.get('stop_requested')

    def publish(self):
        # Serialize reads and publication so a slower worker cannot overwrite
        # newer progress with an older snapshot.
        with self._publish_lock:
            self._publish()

    def _publish(self):
        elapsed = time.monotonic() - self.active if self.active is not None else 0
        budget = dict(mode='adaptive', used_seconds=round(self.used + elapsed, 2),
                      request_count=self.request_count, phase=self.phase,
                      active=self.active is not None, updated_at=now())
        # Worker-owned task dictionaries can change during model/network calls.
        # Publish committed records instead of iterating those mutable dictionaries.
        with self._state_lock:
            ids = {t['id'] for t in self.records}
        records = [public_task(t) for t in self.store.collection_tasks(self.mid) if t['id'] in ids]
        self.service._change(self.mid, self.op, lambda s: s['rounds'][-1].update(
            collections=records, collection_budget=budget), 'collection_progress')

    def begin(self):
        self.active = time.monotonic()
        self.publish()

    def end(self):
        if self.active is not None:
            self.used += time.monotonic() - self.active
            self.active = None
        self.publish()

    def model(self, role, instruction, data):
        if self.stopped():
            raise RuntimeError('用户已停止采集')
        return self.service.provider.json(role, instruction, dict(data, _request_timeout=60))

    def plan(self, tasks, feedback):
        selected = self.state.get('crawl_platforms', [])
        if not selected:
            return []
        comment_questions = [t['question'] for t in tasks if t.get('role') == 'sentiment' or t.get('needs_comments') is True]
        comment_questions.extend(m['content'] for m in feedback if m.get('target') == 'sentiment')
        if self.state['report_type'] == 'opinion' and not comment_questions:
            comment_questions.append(self.state.get('requirements') or '分析相关用户评论的观点与诉求')
        try:
            result = self.model('host', '为本轮问题制定采集计划。返回 {"comment_requirement":{"required":false,"question":"评论需求及范围，无则空"},"collections":[{"platform":"dy/xhs/wb","query":"包含事件主体的简短关键词","question":"待回答问题","condition":"什么原始材料可以回答问题","comments":false,"reason":"采集原因"}]}。'
                '从本轮feedback和tasks判断评论需求，不依赖已有评论数量或角色是否已启用；用户要求网友反应、样本、情绪、诉求时comment_requirement.required=true。'
                '仅使用selected_platforms。首轮覆盖选定平台，按缺口安排关键词。没有舆情或用户观点需求不要采评论；有评论需求时每个已启用平台至少安排一个comments:true查询，复用事件帖子搜索，不要为同一批帖子的评论重复搜索。'
                '评论完成条件需说明问题和来源范围，不能以找到帖子代替取得评论。已存帖子的评论可直接补采。同义词不要重复。最多为每个问题提出一个主要查询，后续不足再修订。',
                dict(title=self.state['title'], selected_platforms=selected, tasks=tasks, feedback=feedback,
                     time_range=self.state['time_range'], requirements=self.state['requirements'], comment_questions=comment_questions))
            need = result.get('comment_requirement') or {}
            if isinstance(need, dict) and need.get('required') is True and need.get('question'):
                comment_questions.append(str(need['question'])[:2000])
            plans = result.get('collections')
            if not isinstance(plans, list):
                raise ValueError('invalid plan')
            plans = [p for p in plans if isinstance(p, dict) and p.get('platform') in selected and isinstance(p.get('query'), str) and p['query'].strip()]
        except Exception:
            self.warnings.append('采集规划未完成，按事件名获取基础材料；需求状态为未评估')
            plans = []
        for platform in selected:
            if not any(p['platform'] == platform for p in plans):
                plans.append(dict(platform=platform, query=self.state['title'], question='；'.join(self.state['agenda']),
                                  condition='取得可对应本轮问题的原始材料', comments=self.state['report_type']=='opinion' or 'sentiment' in self.state['roles']))
            if comment_questions:
                # A fact-only query must not silently satisfy a platform while
                # the same round explicitly requires real comment samples.
                p = next((p for p in plans if p['platform']==platform and p.get('comments') is True),
                         next(p for p in plans if p['platform']==platform))
                p['comments'] = True
                p['comment_question'] = '；'.join(dict.fromkeys(comment_questions))[:2000]
                p['condition'] = (str(p.get('condition') or '取得问题所需材料') + '；取得与以下问题相关的评论原文并核查帖子、平台和日期覆盖：' + p['comment_question'])[:2000]
        return plans

    def task(self, plan):
        saved = self.store.collection_tasks(self.mid)
        old = next((t for t in reversed(saved) if t['platform']==plan['platform'] and t['query']==plan['query'] and t['kind']=='search'), None)
        comments_only = bool(old and not old.get('resumable') and any(t.get('parent_task')==old['id'] and t.get('resumable') for t in saved))
        if old and (old.get('resumable', False) or comments_only):
            task = next((r for r in self.records if r['id']==old['id']), old)
            if not comments_only:
                task.update(status='pending', stop_reason='', message='从已保存断点继续')
            if old.get('round') != self.number:
                task['empty_pages'] = 0
        else:
            task = dict(id=uid('collection'), kind='search', platform=plan['platform'], query=plan['query'][:120], cursor={},
                        post_count=0, comment_count=0, new_evidence_count=0, new_comment_count=0, duplicates=0,
                        excluded=0, request_count=0, empty_pages=0, page=0, seen_ids=[], raw_pages=[], status='pending')
        task.update(platform_name=PLATFORMS[task['platform']], question=str(plan.get('question') or plan.get('reason') or self.state['title'])[:2000],
                    condition=str(plan.get('condition') or '取得问题所需原始依据')[:2000], reason=str(plan.get('reason') or '回答本轮问题')[:1000],
                    comments=bool(plan.get('comments', self.state['report_type']=='opinion')), round=self.number,
                    comment_question=str(plan.get('comment_question') or '')[:2000],
                    scope=SCOPE, assessment=dict(status='unevaluated', reason='尚未评估'), resumable=not comments_only)
        task.pop('_assessed_at', None)
        task.pop('_terminal_assessed', None)
        self.store.save_collection(self.mid, task)
        return task

    def finish_task(self, task, reason, message=None):
        task.update(status=reason, stop_reason=reason, message=message or REASONS.get(reason, reason),
                    resumable=reason not in ('exhausted', 'stalled', 'satisfied', 'model_stopped', 'content_unavailable'), collected_at=now())
        self.store.save_collection(self.mid, task)

    def saved_posts(self, task):
        """Reuse private search cards (including XHS tokens) for accepted old posts."""
        folder = (Path(self.store.root) / self.mid / 'crawls').resolve()
        posts = {}
        accepted = {e.get('source_id') for e in self.evidence if e.get('source_type') == 'social_post'}
        for saved in self.store.collection_tasks(self.mid):
            if saved['platform'] != task['platform'] or saved.get('kind') != 'search' or saved['query'] != task['query']:
                continue
            for raw in saved.get('raw_pages', []):
                path = (Path(self.store.root) / raw / 'result.json').resolve()
                if not path.is_relative_to(folder):
                    continue
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                for post in data.get('posts', []):
                    if task['platform'] + ':post:' + str(post.get('id')) in accepted:
                        posts[post['id']] = post
        return list(posts.values())

    def queue_comments(self, root, posts, queue):
        saved = self.store.collection_tasks(self.mid)
        for post in posts:
            # A platform/post has one live cursor even when several queries find it.
            existing = next((t for t in reversed(self.records + saved) if t.get('kind') == 'comments'
                             and t['platform'] == root['platform'] and t.get('post', {}).get('id') == post['id']), None)
            if existing:
                ids = root.setdefault('comment_task_ids', [])
                if existing['id'] not in ids:
                    ids.append(existing['id'])
                current = next((t for t in self.records if t['id'] == existing['id']), None)
                if current:
                    # A fresh search may renew the XHS token after restoration.
                    current['post'] = post
                    self.store.save_collection(self.mid, current)
                    continue
                if existing['status'] in ('exhausted', 'stalled', 'content_unavailable'):
                    continue
                child = copy.deepcopy(existing)
                child.update(parent_task=root['id'], post=post, status='pending', stop_reason='', message='继续读取已保存帖子的评论',
                             question=root['question'], condition=root['condition'], round=self.number, empty_pages=0, resumable=True)
            else:
                child = dict(id=uid('collection'), parent_task=root['id'], platform=root['platform'], platform_name=root['platform_name'],
                    query=root['query'], question=root['question'], condition=root['condition'], reason='采集相关帖子一级评论', kind='comments', post=post,
                    cursor={}, page=0, status='pending', post_count=0, comment_count=0, new_evidence_count=0, new_comment_count=0,
                    duplicates=0, excluded=0, empty_pages=0, request_count=0, seen_ids=[], raw_pages=[], resumable=True, round=self.number, scope=SCOPE)
                root.setdefault('comment_task_ids', []).append(child['id'])
            queue.append(child)
            with self._state_lock:
                self.records.append(child)
            self.store.save_collection(self.mid, child)
            saved.append(child)
        self.store.save_collection(self.mid, root)

    def ingest(self, task, found, comments):
        # Preserve legacy evidence IDs when the same source changes; storage freezes old versions.
        with self._state_lock:
            sources = {e['source_id']: e for e in self.evidence if e.get('source_id')}
        remap = {}
        for item in found:
            previous = sources.get(item.get('source_id'))
            if previous:
                remap[item['id']] = previous['id']
                item['id'] = previous['id']
                if previous.get('images'):
                    images = {i['id']: i for i in previous['images']}
                    for image in item.get('images', []):
                        if images.get(image['id'], {}).get('status') != 'downloaded' or image.get('status') == 'downloaded':
                            from .images import merge_image
                            images[image['id']] = merge_image(images.get(image['id'], {}), image)
                    item['images'] = list(images.values())
                if item.get('content_scope')=='search_summary' and previous.get('content_scope')!='search_summary':
                    # An intermittent detail failure cannot replace an existing
                    # full text with the shorter search card.
                    item.update(previous)
        for comment in comments:
            comment['evidence_id'] = remap.get(comment['evidence_id'], comment['evidence_id'])
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', self.state['time_range'])
        accepted, excluded = [], []
        for item in found:
            date = re.search(r'\d{4}-\d{2}-\d{2}', item.get('date', ''))
            if len(dates)==2 and date and not dates[0] <= date[0] <= dates[1]:
                excluded.append(dict(id=item['id'], reason='来源日期不在指定范围'))
            else:
                item['date_scope'] = '来源日期已记录，内容覆盖期仍需核验' if date else '来源日期未知，未确认满足时间范围'
                accepted.append(item)
        # Comments inherit the relevance of their accepted parent; search pages are assessed in bounded contexts.
        if task['kind'] == 'search' and accepted:
            # Text-only relevance cannot judge an image-only post. Keep it explicitly
            # pending visual extraction instead of silently discarding its attachments.
            relevant = [e for e in accepted if e.get('content_scope')=='image_only'
                        or (e.get('images') and e['text']=='图片帖（尚未识别图片内容）')]
            for e in relevant:
                e['relevance_status'] = 'awaiting_image_analysis'
            accepted = [e for e in accepted if e not in relevant]
            for start in range(0, len(accepted), 8):
                batch = accepted[start:start+8]
                try:
                    result = self.model('review', '判断原始资料与指定事件和问题的相关性，返回 {"accepted_ids":["证据ID"],"reasons":{}}。排除同名人物和无关事件；仅做相关性判断，不把内容当作事实。',
                                        dict(title=self.state['title'], question=task['question'], evidence=batch))
                    ids = result.get('accepted_ids')
                    if not isinstance(ids, list) or not set(ids) <= {e['id'] for e in batch}:
                        raise ValueError('invalid relevance')
                    relevant.extend(e for e in batch if e['id'] in ids)
                    excluded.extend(dict(id=e['id'], reason=str((result.get('reasons') or {}).get(e['id'], '与议题无关'))) for e in batch if e['id'] not in ids)
                except Exception:
                    # Retain unassessed input separately; resume it before requesting another page.
                    task['pending_ingest'] = dict(found=accepted[start:], comments=comments)
                    self.warnings.append('相关性筛选未完成；原始资料保留，下一轮可继续处理')
                    break
            accepted = relevant
        if excluded:
            task.setdefault('exclusions', []).extend(excluded)
            task['excluded'] += len(excluded)
        with self._state_lock:
            existing = {e['id'] for e in self.evidence}
            fresh = [e for e in accepted if e['id'] not in existing]
            updates = {e['id']: e for e in accepted if e['id'] in existing and
                       (e['text'], e.get('date'), e.get('images')) != (sources.get(e.get('source_id'), {}).get('text'), sources.get(e.get('source_id'), {}).get('date'), sources.get(e.get('source_id'), {}).get('images'))}
            hashes = {hashlib.sha256(e['text'].strip().encode()).hexdigest(): e['id'] for e in self.evidence}
            for e in fresh:
                digest = hashlib.sha256(e['text'].strip().encode()).hexdigest()
                if digest in hashes and e.get('relevance_status')!='awaiting_image_analysis':
                    e['duplicate_of'] = hashes[digest]
                hashes.setdefault(digest, e['id'])
            available = existing | {e['id'] for e in accepted}
            accepted_comments = [c for c in comments if c['evidence_id'] in available]
            current_comments = {c['id']: c for c in self.state['comments']}
            def persist(s):
                s['evidence'] = [updates.get(e['id'], e) for e in s['evidence']]
                known = {e['id'] for e in s['evidence']}
                s['evidence'].extend(e for e in fresh if e['id'] not in known)
                by_id = {c['id']: c for c in s['comments']}
                by_id.update({c['id']: c for c in accepted_comments})
                s['comments'] = list(by_id.values())
                if accepted_comments and 'sentiment' not in s['roles']:
                    s['roles'].append('sentiment')
            self.service._change(self.mid, self.op, persist, 'evidence')
            self.evidence[:] = [updates.get(e['id'], e) for e in self.evidence]
            self.evidence.extend(fresh)
            task['new_evidence_count'] += len(fresh)
            task['new_comment_count'] += sum(c['id'] not in current_comments for c in accepted_comments)
            current_comments.update({c['id']: c for c in accepted_comments})
            self.state['comments'] = list(current_comments.values())
            checkpoints.write(self.service.round_folder(self.state) / 'acquired.json',
                              dict(evidence=self.evidence, comments=self.state['comments']))
            posts = {e['source_id'].split(':')[-1] for e in accepted if e['source_type']=='social_post'}
            task['accepted_post_ids'] = sorted(set(task.get('accepted_post_ids', [])) | posts)
            return posts

    def requirement_progress(self, task):
        """A search page ending does not mean its queued comment work has ended."""
        saved = self.store.collection_tasks(self.mid)
        children = [t for t in saved if t.get('parent_task')==task['id'] or t['id'] in task.get('comment_task_ids', [])]
        active = [t for t in children if t['status'] in ('pending', 'running')]
        parents = set(task.get('accepted_post_ids', [])) | {t.get('post', {}).get('id') for t in children}
        with self._state_lock:
            comments = [c for c in self.state['comments'] if c.get('platform')==task['platform'] and c.get('parent_id') in parents]
            all_comments = list(self.state['comments'])
        post_counts = Counter(c.get('parent_id') for c in comments)
        dates = Counter((re.search(r'\d{4}-\d{2}-\d{2}', c.get('time', '')) or ['日期未知'])[0] for c in comments)
        coverage = dict(post_count=len(post_counts), date_counts=dict(dates),
                        largest_post_comments=max(post_counts.values(), default=0),
                        unique_text_count=len({re.sub(r'\s+', '', c['text']).casefold() for c in comments}))
        task['comment_coverage'] = coverage
        stats = public_task(task)
        stats.update(comment_count=task.get('comment_count', 0)+sum(t.get('comment_count', 0) for t in children),
                     valid_comment_count=len(comments), pending_comment_tasks=len(active),
                     completed_comment_tasks=sum(t['status']=='exhausted' for t in children),
                     comment_failures=[dict(status=t['status'], message=t.get('message', '')) for t in children
                                       if t['status'] not in ('pending', 'running', 'exhausted', 'satisfied', 'model_stopped')],
                     search_can_continue=task['status'] in ('pending', 'running'),
                     meeting_comment_sources=dict(Counter(c.get('platform', c.get('source', '未知')) for c in all_comments)),
                     platform_blocks=dict(self.blocked_platforms),
                     consecutive_empty_batches=task.get('empty_refinements', 0))
        return stats

    def assess(self, roots):
        # Each platform owns its roots; assessment never holds a global network lock.
        for task in list(roots):
            if self.stopped() or task['status'] not in ('pending', 'exhausted', 'stalled'):
                continue
            progress = self.requirement_progress(task)
            terminal = not progress['search_can_continue'] and not progress['pending_comment_tasks']
            if not terminal and time.monotonic()-task.get('_assessed_at', float('-inf')) < 30:
                continue
            signature = (task.get('page', 0), progress['comment_count'], progress['completed_comment_tasks'], len(progress['comment_failures']))
            if terminal and task.get('_terminal_assessed') == list(signature):
                continue
            task['_assessed_at'] = time.monotonic()
            task['_terminal_assessed'] = list(signature) if terminal else None
            self.decide(task, can_continue=not terminal, progress=progress)
        self.publish()

    def decide(self, task, can_continue=False, progress=None):
        progress = progress or self.requirement_progress(task)
        with self._state_lock:
            evidence = retrieve(task['question']+' '+task['condition'], list(self.evidence), 8)
            if task.get('comments'):
                parents = set(task.get('accepted_post_ids', [])) | {t.get('post', {}).get('id') for t in self.store.collection_tasks(self.mid)
                    if t.get('parent_task') == task['id'] or t['id'] in task.get('comment_task_ids', [])}
                comment_ids = {c.get('evidence_id') for c in self.state['comments']
                    if c.get('platform') == task['platform'] and c.get('parent_id') in parents}
                comment_evidence = retrieve(task.get('comment_question') or task['question'],
                    [e for e in self.evidence if e['id'] in comment_ids], 8)
                evidence = list({e['id']: e for e in evidence + comment_evidence}.values())
        try:
            data = self.model('host', '根据本项采集结果决定下一步，返回 {"covered":false,"action":"continue/refine/stop",'
                '"evidence_ids":[],"reason":"具体判断理由","gaps":[],"next_query":"调整后的简短关键词，否则空字符串"}。'
                '不设整轮时间或帖子数量上限。材料已覆盖时covered=true、action=stop，必须引用给定原文并符合完成条件，后续仍需核验。'
                '材料不足且分页可用可continue；有明确新检索方向时refine并提供未尝试关键词；来源耗尽或无有效方向时stop并记录缺口。'
                '搜索末页不等于评论已读完，pending_comment_tasks表示尚可读取的评论任务；有评论需求时应结合valid_comment_count判断，不能使用其他查询的评论充数。'
                'comment_coverage给出评论来自多少帖子、日期分布、文本去重数和单帖集中程度。只有同一帖子的几条评论不能满足跨帖子或跨平台比较；仍有待采帖子时优先继续评论分页。'
                '评论需求covered=true必须引用本查询取得的评论原文，不能只引帖子或报道。结合用户需要决定继续、换关键词或停止，不机械凑样本数。'
                '其他平台受阻时结合meeting_comment_sources与platform_blocks判断本平台可补充的样本；无法满足的范围写入gaps，不能把来源耗尽写成样本充分。'
                '重复或空结果要说明具体缺口；只有存在实质新方向才继续改词。可采集网页和帖子文字、一级评论及帖子配图；已保存的相关配图会进入视觉识别。不能用反复文字搜索假装已读图片；不支持视频转写。'
                '不要无目的改写同义关键词。转发数量不能代替原始发布。不能把未检索到当作不存在。',
                dict(question=task['question'], condition=task['condition'], evidence=evidence,
                     time_range=self.state['time_range'], statistics=progress, can_continue=can_continue,
                     attempted_queries=[t['query'] for t in self.records if t['platform']==task['platform']]))
            ids = data.get('evidence_ids', [])
            if not isinstance(data.get('covered'), bool) or not isinstance(ids, list) or not all(isinstance(x,str) for x in ids):
                raise ValueError('invalid assessment')
            valid = set(ids) <= {e['id'] for e in evidence}
            covered = data['covered'] and valid and bool(ids)
            if data['covered'] and not covered:
                raise ValueError('coverage without valid references')
            if task.get('comments') and data['covered'] and (not progress['valid_comment_count'] or not set(ids) & comment_ids):
                covered = False
                data['reason'] = '评论需求尚未引用本查询的有效评论原文，不能仅凭帖子标记已覆盖'
                data['action'] = 'continue' if can_continue else 'stop'
            query = str(data.get('next_query') or '').strip()[:120]
            action = data.get('action') or ('refine' if query else 'continue' if can_continue else 'stop')
            if action not in ('continue','refine','stop') or not data.get('reason'):
                raise ValueError('invalid decision')
            task['assessment'] = dict(status='covered' if covered else 'gaps', action='stop' if covered else action,
                reason=str(data['reason'])[:2000], evidence_ids=ids if valid else [],
                gaps=data.get('gaps', []) if isinstance(data.get('gaps'), list) else [], at=now())
            task.setdefault('decisions', []).append(copy.deepcopy(task['assessment']))
            if covered or action=='stop':
                reason = 'satisfied' if covered else 'model_stopped'
                for other in self.records:
                    if other['id']==task['id'] or other.get('parent_task')==task['id']:
                        # Preserve actual exhaustion/failure diagnostics.
                        if other['status'] in ('pending','running','complete'):
                            self.finish_task(other, reason)
                if covered:
                    self.finish_task(task, 'satisfied')
            elif action=='refine':
                attempted = {self.query_key(t['query']) for t in self.records if t['platform']==task['platform']}
                if not query or self.query_key(query) in attempted:
                    task['assessment']['refinement_blocked'] = '查询为空或已执行，未重复请求'
                    if task['status'] not in ('exhausted','stalled'):
                        self.finish_task(task, 'stalled')
                else:
                    task['suggested_query'] = query
                    if task['status'] not in ('exhausted','stalled'):
                        self.finish_task(task, 'model_stopped')
                    for other in self.records:
                        if other.get('parent_task')==task['id'] and other['status']=='pending':
                            self.finish_task(other, 'model_stopped')
            elif not can_continue and task['status'] not in ('exhausted','stalled'):
                self.finish_task(task, 'exhausted')
        except Exception as exc:
            task['assessment'] = dict(status='unevaluated', reason='需求评估失败，暂停该需求；可下一轮重试，不判定材料充分', at=now())
            task['error_type'] = type(exc).__name__
            self.finish_task(task, 'user_stopped' if self.stopped() else 'assessment_failed')
            for other in self.records:
                if other.get('parent_task')==task['id'] and other['status']=='pending':
                    self.finish_task(other, 'assessment_failed')
        self.store.save_collection(self.mid, task)

    @staticmethod
    def query_key(query):
        return re.sub(r'[^\w\u4e00-\u9fff]', '', query).lower()

    def run(self, plans=None, tasks=None, feedback=None, resume_ids=None, web_queries=None, initial=False):
        self.phase = 'initial' if initial else 'supplement'
        self.begin()
        try:
            if self.stopped():
                return []
            plans = plans if plans is not None else ([] if resume_ids else self.plan(tasks or [], feedback or []))
            roots = []
            for plan in plans:
                same = next((t for t in roots if t['platform']==plan['platform'] and t['query']==plan['query']), None)
                if same:
                    same['comments'] = same.get('comments', False) or bool(plan.get('comments'))
                    same['question'] += '；' + str(plan.get('question', ''))
                    same['condition'] += '；' + str(plan.get('condition', ''))
                    self.store.save_collection(self.mid, same)
                else:
                    roots.append(self.task(plan))
            resumed = []
            saved = self.store.collection_tasks(self.mid)
            for tid in resume_ids or []:
                task = next(t for t in saved if t['id']==tid)
                task.update(status='pending', stop_reason='', round=self.number, empty_pages=0, cursor_reset_this_run=False)
                task.pop('_assessed_at', None)
                task.pop('_terminal_assessed', None)
                resumed.append(task)
                if task['kind']=='search':
                    roots.append(task)
            # Start one query on each platform before a second query on any platform.
            initial = resumed or roots
            first, rest, seen_platforms = [], [], set()
            for task in initial:
                (rest if task['platform'] in seen_platforms else first).append(task)
                seen_platforms.add(task['platform'])
            queue = deque(first + rest)
            for task in initial:
                if not any(r['id']==task['id'] for r in self.records):
                    self.records.append(task)
            # Restore comment page cursors after all initial search sources have been scheduled.
            root_ids = {r['id'] for r in roots}
            for t in self.store.collection_tasks(self.mid):
                if t.get('parent_task') in root_ids and t.get('resumable') and not any(r['id']==t['id'] for r in self.records):
                    t.update(status='pending', round=self.number)
                    queue.append(t)
                    self.records.append(t)
            for root in roots:
                if root.get('comments'):
                    self.queue_comments(root, self.saved_posts(root), queue)
            # One worker per platform: no same-platform page overlap. The single
            # clock started above measures elapsed wall time, not the sum of workers.
            by_platform = {}
            for task in queue:
                by_platform.setdefault(task['platform'], deque()).append(task)
            with ThreadPoolExecutor(max_workers=len(PLATFORMS)+1, thread_name_prefix='collection') as pool:
                futures = [pool.submit(self._run_platform, [r for r in roots if r['platform']==platform], work)
                           for platform, work in by_platform.items()]
                if web_queries:
                    futures.append(pool.submit(self._run_web, list(dict.fromkeys(web_queries))))
                for future in futures:
                    future.result()
            for task in self.records:
                if task.get('stop_reason') not in ('satisfied', 'response_received'):
                    note = task['platform_name'] + '：' + task.get('message', '')
                    if note not in self.warnings:
                        self.warnings.append(note)
            return [public_task(t) for t in self.records]
        finally:
            self.end()
            self.phase = 'supplement'

    def _run_platform(self, roots, queue):
        visited = set()
        while queue and not self.stopped():
            task = queue.popleft()
            if task['status'] not in ('pending', 'running'):
                continue
            if task['platform'] in self.blocked_platforms:
                self.finish_task(task, self.blocked_platforms[task['platform']])
                continue
            task.update(status='running', message='正在处理下一页')
            self.store.save_collection(self.mid, task)
            self.publish()
            try:
                previous_new = task['new_evidence_count']
                pending = task.pop('pending_ingest', None)
                if pending:
                    allowed = self.ingest(task, pending['found'], pending['comments'])
                    result = task.pop('pending_result', dict(status='complete', has_more=True, posts=[]))
                else:
                    if self.stopped():
                        self.finish_task(task, 'user_stopped' if self.stopped() else 'failed')
                        continue
                    result, found, comments = self.service.crawler.collect_page(self.mid, self.number, task, 90, self.stopped)
                    task['request_count'] += result.get('request_count', 0)
                    with self._state_lock:
                        self.request_count += result.get('request_count', 0)
                    if result.get('raw_page'):
                        task.setdefault('raw_pages', []).append(result['raw_page'])
                    task['cursor'] = result.get('cursor', task.get('cursor', {}))
                    task['page'] = result.get('page', task['page'])
                    task['post_count'] += len(result.get('posts', []))
                    task['comment_count'] += len(result.get('comments', []))
                    if result.get('item_failures'):
                        task.setdefault('item_failures', []).extend(result['item_failures'])
                    for key in ('error_type', 'result_diagnostic'):
                        if result.get(key):
                            task[key] = result[key]
                    ids = {e.get('source_id') or e['id'] for e in found}
                    old = set(task.get('seen_ids', []))
                    task['duplicates'] += len(ids & old)
                    task['seen_ids'] = list(old | ids)
                    allowed = self.ingest(task, found, comments)
                if task.get('pending_ingest'):
                    task['pending_result'] = result
                    self.finish_task(task, 'user_stopped' if self.stopped() else 'failed')
                    continue
                task['empty_pages'] = 0 if task['new_evidence_count']>previous_new else task.get('empty_pages', 0)+1
                if task['kind']=='search' and task.get('comments'):
                    self.queue_comments(task, [post for post in result.get('posts', []) if post['id'] in allowed
                        or any(e.get('source_id') == task['platform']+':post:'+post['id'] for e in self.evidence)], queue)
                failure = result.get('failure_status') or (result['status'] if result['status']!='complete' else '')
                if failure == 'cursor_invalid' and not task.get('cursor_reset_this_run'):
                    task.setdefault('cursor_resets', []).append(dict(at=now(), page=task['page'], reason=REASONS['cursor_invalid']))
                    task.update(cursor={}, cursor_reset_this_run=True, status='pending', empty_pages=0,
                                message='分页断点失效，已记录；从第一页重新查询并去重')
                    self.store.save_collection(self.mid, task)
                    queue.append(task)
                elif failure:
                    self.finish_task(task, failure, result.get('message'))
                    # A rejected session should not be retried for every comment on the same platform.
                    if failure in ('needs_login', 'rate_limited', 'busy', 'unavailable'):
                        self.blocked_platforms[task['platform']] = failure
                        for other in list(queue):
                            if other['platform']==task['platform']:
                                self.finish_task(other, failure)
                                queue.remove(other)
                elif result.get('repeated_cursor') or task['empty_pages'] >= 3:
                    self.finish_task(task, 'stalled')
                elif not result.get('has_more'):
                    self.finish_task(task, 'exhausted')
                else:
                    task.update(status='pending', message='本页完成，等待轮转')
                    queue.append(task)
                    self.store.save_collection(self.mid, task)
                visited.add(task['platform'])
                if visited >= {t['platform'] for t in roots if t['status'] not in ('needs_login','failed','busy','unavailable')}:
                    self.assess(roots)
                    visited.clear()
                    for root in list(roots):
                        query = root.pop('suggested_query', '')
                        if query and not any(t['query']==query and t['platform']==root['platform'] for t in self.records):
                            extra = self.task(dict(root, query=query))
                            extra['empty_refinements'] = (root.get('empty_refinements', 0)+1) if root['new_evidence_count']==0 else 0
                            roots.append(extra)
                            with self._state_lock:
                                self.records.append(extra)
                            queue.append(extra)
            except Exception as exc:
                task['error_type'] = type(exc).__name__
                self.finish_task(task, 'failed')
            self.publish()
        for task in queue:
            if task['status']!='satisfied':
                self.finish_task(task, 'user_stopped' if self.stopped() else 'failed')

    def _run_web(self, queries):
        for query in queries:
            try:
                found = self.search(query, within_round=True)
                with self._state_lock:
                    seen = {e['id'] for e in self.evidence}
                    added = [e for e in found if e['id'] not in seen]
                    def persist(s):
                        known = {e['id'] for e in s['evidence']}
                        s['evidence'].extend(e for e in added if e['id'] not in known)
                    self.service._change(self.mid, self.op, persist, 'evidence')
                    self.evidence.extend(added)
                    checkpoints.write(self.service.round_folder(self.state) / 'acquired.json',
                                      dict(evidence=self.evidence, comments=self.state['comments']))
            except Exception:
                self.warnings.append('联网检索未完成，其他平台继续；详见采集任务记录')

    def search(self, query, within_round=False, question=None, condition=None):
        """Search/refine under coordinator control; retain every successful batch."""
        if not within_round:
            self.phase = 'supplement'
            self.begin()
        session = uid('search-session')
        original, collected, empty_batches, attempted = query, {}, 0, set()
        try:
            while self.query_key(query) not in attempted:
                attempted.add(self.query_key(query))
                found = self._search_once(query, within_round=True)
                task = next(t for t in reversed(self.records) if t['kind']=='web' and t['query']==query)
                task.update(search_session_id=session, question=question or original,
                    condition=condition or '取得回答问题所需的网页材料；若问题要求官网、原始发布或原文，须核对实际来源身份和正文，媒体转述不能代替原件')
                with self._state_lock:
                    seen = {e['id'] for e in self.evidence}
                    added = [e for e in found if e['id'] not in seen]
                    self.evidence.extend(added)
                    collected.update({e['id']: e for e in found})
                    def persist(s):
                        known = {e['id'] for e in s['evidence']}
                        s['evidence'].extend(e for e in added if e['id'] not in known)
                    self.service._change(self.mid, self.op, persist, 'evidence')
                    checkpoints.write(self.service.round_folder(self.state) / 'acquired.json',
                                      dict(evidence=self.evidence, comments=self.state['comments']))
                empty_batches = 0 if added else empty_batches + 1
                if self.stopped():
                    self.finish_task(task, 'user_stopped')
                    break
                if task['status'] != 'complete':
                    break
                task['empty_refinements'] = empty_batches
                self.decide(task, can_continue=False)
                query = task.pop('suggested_query', '')
                self.store.save_collection(self.mid, task)
                self.publish()
                if not query:
                    break
            return list(collected.values())
        finally:
            if not within_round:
                self.end()

    def _search_once(self, query, within_round=False):
        if not within_round:
            self.begin()
        task = dict(id=uid('collection'), kind='web', platform='web', platform_name='联网检索',
                    query=query, question=query, condition='取得与问题相关的网页依据', round=self.number,
                    status='running', page=1, post_count=0, comment_count=0, new_evidence_count=0,
                    new_comment_count=0, duplicates=0, excluded=0, request_count=0, resumable=False,
                    scope='搜索摘要与自动提取的网页文字分别入库；正文读取失败保留原因，不代表资料已穷尽')
        with self._state_lock:
            self.records.append(task)
        self.store.save_collection(self.mid, task)
        self.publish()
        try:
            if self.stopped():
                self.warnings.append('联网补证未执行：用户已停止采集')
                self.finish_task(task, 'user_stopped' if self.stopped() else 'failed')
                return []
            with self._state_lock:
                self.request_count += 1
            task['request_count'] = 1
            page_start = len(self.web_fetches)
            diagnostics = {}
            found = self.service._search_unbudgeted(query, self.state, self.warnings, diagnostics)
            task.update(diagnostics)
            task['search_providers'] = sorted({e['search_provider'] for e in found if e.get('search_provider')})
            task['web_pages'] = self.web_fetches[page_start:]
            task.update(status='complete', stop_reason='response_received', message='本次搜索已完成；不代表网络资料穷尽',
                        returned_count=len(found),
                        duplicates=len({e['id'] for e in found}&{e['id'] for e in self.evidence}),
                        new_evidence_count=len({e['id'] for e in found}-{e['id'] for e in self.evidence}), collected_at=now())
            if diagnostics.get('pending_relevance'):
                self.finish_task(task, 'user_stopped' if self.stopped() else 'failed')
                task['message'] += '；部分搜索结果尚未完成相关性筛选，原始结果已保存'
            return found
        except Exception:
            self.finish_task(task, 'failed')
            raise
        finally:
            task['resumable'] = False  # Web adapters have no supported page cursor; a follow-up issues a new query.
            self.store.save_collection(self.mid, task)
            if not within_round:
                self.end()
            else:
                self.publish()
