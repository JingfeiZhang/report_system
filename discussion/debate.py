"""Bounded, evidence-grounded debate with isolated proposal and revision barriers.

All persisted objects are public analysis summaries, never private reasoning traces.
The runner uses the meeting's existing operation lease and frozen round input.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

from .evidence import retrieve
from .store import uid
from . import checkpoints


def evidence_context(evidence, context, required, query, top_k=10):
    """Supply actual passages for every referenced ID; fail rather than truncate them."""
    refs = set(re.findall(r"E-[a-f0-9]{12}", json.dumps(context, ensure_ascii=False)))
    refs.update(required or [])
    by_id = {e['id']: e for e in evidence}
    refs.update(by_id[eid].get('parent_evidence_id') for eid in list(refs)
                if eid in by_id and by_id[eid].get('source_type')=='image_analysis')
    if not refs <= by_id.keys():
        raise RuntimeError('上下文引用缺少原文，任务未执行')
    selected = [e for e in evidence if e['id'] in refs]
    size = sum(len(e['text']) for e in selected)
    if size > 48000:
        raise RuntimeError('必需证据超过上下文预算，请拆分议题后重试')
    for e in retrieve(query, evidence, 10)[:top_k] if top_k else []:
        related = [e]
        if e.get('source_type')=='image_analysis':
            parent = by_id.get(e.get('parent_evidence_id'))
            if parent is None:
                continue
            related.append(parent)
        related = [item for item in related if item['id'] not in refs]
        if size + sum(len(item['text']) for item in related) <= 48000:
            selected.extend(related)
            refs.update(item['id'] for item in related)
            size += sum(len(item['text']) for item in related)
    return selected


def revision(evidence):
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def complete_verification(card, context):
    """Only cache complete, well-formed batches; incomplete responses remain retryable."""
    checks = card.get('verification_data', [])
    if not isinstance(checks, list) or not isinstance(card.get('reply_data', []), list):
        return False
    required = set(context.get('required_claim_ids', []))
    valid = [v['claim_id'] for v in checks if isinstance(v, dict) and v.get('claim_id') in required
             and v.get('status') in ('supported', 'partial', 'insufficient', 'conflicting', 'refuted')
             and v.get('reason')]
    challenges = {c['id'] for r in context.get('peer_reviews', []) for c in r.get('challenges', [])}
    replies = [r.get('challenge_id') for r in card.get('reply_data', []) if isinstance(r, dict)
               and isinstance(r.get('resolved'), bool) and r.get('reason')]
    return set(valid) == required and len(valid) == len(required) and challenges <= set(replies)


ANALYSIS_KINDS = {
    'fact_check': '关键事实需要独立核查及互审',
    'viewpoint_comparison': '不同来源的解读需要独立比较、相互质疑',
    'sentiment_interpretation': '观点解释与样本偏差需要独立分析、相互复核',
    'aggregation': '仅汇总程序统计，由核验角色检查口径',
    'source_digest': '仅整理来源与摘要，由核验角色检查归属',
}
DEFAULT_KIND = {'facts': 'fact_check', 'media': 'viewpoint_comparison', 'sentiment': 'sentiment_interpretation'}
ANALYSIS_FOCUS = {
    'facts': [('事实与时间线', '梳理时间、主体和论据关系，独立核查关键事实。'),
              ('来源与反证', '独立核查同一事实，优先寻找反对证据、归因错误及其他解释。')],
    'media': [('报道与观点', '比较报道的关注点、观点归属与论据，形成独立解释。'),
              ('叙事与来源复核', '独立解释同一组报道，检查转载依赖、选择性呈现和替代解释。不能预设媒体立场。')],
    'sentiment': [('情绪与诉求', '结合评论或帖子分析情绪、诉求及可能解释。'),
                  ('样本与解释复核', '独立分析同一组材料，检查样本偏差、反例、讽刺语义和跨平台比较是否成立。')],
}


def configure_collaboration(task):
    """Route by task type, never by its position in the coordinator's list."""
    kind = task.get('analysis_kind')
    if not isinstance(kind, str) or kind not in ANALYSIS_KINDS:
        kind = DEFAULT_KIND[task['role']]
    paired_role = task['role'] in ('media', 'sentiment')
    debate = paired_role or task.get('debate') is True or kind not in ('aggregation', 'source_digest')
    reason = (str(task.get('reason') or '')[:400] if task.get('debate') is debate else '')
    if not reason:
        reason = '任务要求独立作答后互相复核' if debate and kind in ('aggregation', 'source_digest') else ANALYSIS_KINDS[kind]
    if paired_role:
        reason = '该议题由两个独立实例作答并互相质询，来源整理与统计汇总也须检查依据及解释'
    return dict(task, analysis_kind=kind, debate=debate, reason=reason, collaboration_version=2)


class IncompleteExchange(RuntimeError):
    def __init__(self, message, validation=None):
        super().__init__(message)
        self.validation = validation or {'errors': [message]}


def valid_reply(reply, challenge_id):
    return (isinstance(reply, dict) and reply.get('challenge_id') == challenge_id
            and reply.get('action') in ('maintain', 'qualify', 'revise', 'withdraw')
            and isinstance(reply.get('response'), str) and bool(reply['response'].strip()))


def reply_validation(replies, required):
    """Return repairable IDs and valid answers without accepting duplicates/unknowns."""
    items = replies if isinstance(replies, list) else []
    counts = Counter(r['challenge_id'] for r in items if isinstance(r, dict) and isinstance(r.get('challenge_id'), str))
    valid = [r for r in items if isinstance(r, dict) and isinstance(r.get('challenge_id'), str)
             and r['challenge_id'] in required and counts[r['challenge_id']] == 1 and valid_reply(r, r['challenge_id'])]
    missing = sorted(required - {r['challenge_id'] for r in valid})
    errors = []
    if missing:
        errors.append('以下质询缺少唯一、有效的回应：' + '、'.join(missing))
    if not isinstance(replies, list):
        errors.append('replies必须为数组')
    for index, item in enumerate(items):
        cid = item.get('challenge_id') if isinstance(item, dict) else None
        if not isinstance(cid, str) or cid not in required:
            errors.append(f'replies[{index}]的challenge_id不在本次待答清单中')
        elif counts[cid] > 1:
            errors.append(cid + '重复回应，必须恰好一项')
        elif not valid_reply(item, cid):
            errors.append(cid + '缺少非空response或有效action（maintain/qualify/revise/withdraw）')
    return dict(missing_challenge_ids=missing, errors=errors), valid


def validate_exchange(card, task, context):
    """A successful model request alone does not prove reciprocal dialogue happened."""
    if not task.get('require_reciprocal'):
        return
    if task.get('phase') == 'peer_review':
        review = card.get('review_data')
        challenges = review.get('challenges') if isinstance(review, dict) else None
        valid_ids = {c['id'] for c in context['target']['claims']}
        if not isinstance(challenges, list) or (task['exchange'] == 1 and not challenges):
            raise IncompleteExchange('互审未返回针对对方主张的具体质询，不能作为已完成辩论')
        if len(challenges) > 2 or any(not isinstance(c, dict) or c.get('claim_id') not in valid_ids or
                not all(isinstance(c.get(k), str) and c[k].strip() for k in ('question','reason','resolution')) for c in challenges):
            raise IncompleteExchange('互审质询缺少有效主张、理由或解决条件')
    if task.get('phase') == 'revision':
        required = {c['id'] for r in context['peer_reviews'] for c in r['challenges'] if c['target_agent']==task['agent_id']}
        validation, _ = reply_validation(card.get('reply_data'), required)
        if validation['errors']:
            raise IncompleteExchange(f"第{task['exchange']}次交锋修订未通过回应校验，已保留上一版答案", validation)


class DebateRunner:
    def __init__(self, service, state, op, evidence, sentiment, warnings):
        self.service, self.state, self.op = service, state, op
        self.evidence, self.sentiment, self.warnings = evidence, sentiment, warnings
        self.mid = state['id']
        self.cards, self.issues, self.reviews, self.verifications, self.tools = [], [], [], [], []
        self.calls = 0
        self.limit = 32
        self.searches = set()
        self.platform_searches = set()
        self.folder = service.round_folder(state)
        self.reserve = 0
        self.verification_reserves = {}

    def stable_id(self, prefix, value):
        return prefix + '-' + checkpoints.digest([self.mid, len(self.state['rounds']), value])[:12]

    def exchange_stopped(self):
        return self.stopped() or self.service.store.get(self.mid)['rounds'][-1].get('exchange_stop_requested', False)


    def save(self, kind='debate_progress'):
        data = copy.deepcopy(dict(issues=self.issues, peer_reviews=self.reviews,
                                  verifications=self.verifications, tool_runs=self.tools,
                                  budget={'analysis_calls': self.calls, 'analysis_limit': self.limit,
                                          'max_exchanges': self.state.get('debate_exchanges', 2),
                                          'scope': '提案、互审、修订、核验；规划、检索筛选、评论分类、报告另计'}))
        self.service._change(self.mid, self.op, lambda s: s['rounds'][-1].update(data), kind)

    def stage(self, text, code):
        self.service._change(self.mid, self.op,
                             lambda s: s['rounds'][-1].update(stage=text, phase=code), code)

    def stopped(self):
        return self.service.store.get(self.mid)['rounds'][-1].get('stop_requested', False)

    def task(self, role, question, **kwargs):
        task = dict(id=self.stable_id('task', [role, question, kwargs]), role=role, question=question, feedback_ids=[], status='pending', error='')
        task.update(kwargs)
        def register(s):
            if not any(t['id'] == task['id'] for t in s['rounds'][-1]['tasks']):
                s['rounds'][-1]['tasks'].append(copy.deepcopy(task))
        self.service._change(self.mid, self.op, register, 'tasks')
        return task

    def batch(self, jobs):
        # Reserve before dispatch. Results are never fed into siblings in the same batch.
        allowance = self.limit - self.calls
        if jobs and jobs[0][0].get('phase') in ('peer_review', 'revision'):
            allowance -= self.reserve
        accepted = jobs[:max(0, allowance)]
        if len(accepted) < len(jobs):
            self.warnings.append('分析调用预算耗尽，未完成的互审或修订需下一轮继续')
            for task, _ in jobs[len(accepted):]:
                self.service._task_status(self.mid, self.op, task['id'], 'failed', '分析预算耗尽')
        self.calls += len(accepted)
        self.save()
        output = []
        def execute(task, context):
            if self.stopped():
                raise RuntimeError('用户已终止本轮，未发起模型请求')
            cache_path = self.folder / 'calls' / (task['id'] + '.json')
            identity = (self.service.provider.call_identity(task['role'])
                        if hasattr(self.service.provider, 'call_identity') else type(self.service.provider).__name__)
            fingerprint_parts = [task, context, revision(self.evidence), self.sentiment, identity]
            if task.get('phase') == 'revision':
                fingerprint_parts.append('individual-replies-v1')
            fingerprint = checkpoints.digest(fingerprint_parts)
            cached = checkpoints.read(cache_path)
            if cached and cached['fingerprint'] == fingerprint and (task.get('phase') != 'verification'
                    or complete_verification(cached['card'], context)):
                try:
                    validate_exchange(cached['card'], task, context)
                    return copy.deepcopy(cached['card'])
                except IncompleteExchange:
                    pass
            self.service._task_status(self.mid, self.op, task['id'], 'running')
            start = time.monotonic()
            attempts = 2
            request_context = copy.deepcopy(context)
            draft_path = self.folder / 'drafts' / (task['id'] + '.json')
            draft_checkpoint = checkpoints.read(draft_path) if task.get('phase') == 'revision' else None
            if draft_checkpoint and draft_checkpoint.get('fingerprint') == fingerprint:
                request_context.update(copy.deepcopy(draft_checkpoint['repair_context']))
                attempts = 1
            request_count = 0
            for attempt in range(1, attempts + 1):
                capture = {}
                try:
                    if self.stopped():
                        raise RuntimeError('用户已终止本轮，未发起模型请求')
                    card = self.service._card(self.state, task, self.evidence, self.sentiment, copy.deepcopy(request_context), capture=capture)
                    validate_exchange(card, task, context)
                    if task.get('strict_verification') and not complete_verification(card, context):
                        raise IncompleteExchange('核验输出遗漏主张或质疑检查项，当前批次需重试')
                    break
                except Exception as exc:
                    from .service import error_text
                    validation = getattr(exc, 'validation', {'errors': [error_text(exc)]})
                    # Save rejected public JSON, never request credentials or private reasoning.
                    checkpoints.write(self.folder / 'failures' / f"{task['id']}-{attempt}-{time.time_ns()}.json",
                        dict(task_id=task['id'], phase=task.get('phase'), attempt=attempt,
                             input_fingerprint=fingerprint, model_output=capture.get('model_output'), validation=validation))
                    if isinstance(exc, IncompleteExchange):
                        def diagnostic(s):
                            t = next(t for t in s['rounds'][-1]['tasks'] if t['id'] == task['id'])
                            t['response_validation'] = copy.deepcopy(validation)
                        self.service._change(self.mid, self.op, diagnostic, 'task_status')
                    retryable = isinstance(exc, IncompleteExchange) or any(x in str(exc) for x in ('Timeout', 'Connection', 'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504'))
                    if attempt == attempts or not retryable or self.stopped():
                        raise
                    if isinstance(exc, IncompleteExchange) and task.get('phase') == 'revision' and capture.get('model_output'):
                        required = {c['id'] for r in context['peer_reviews'] for c in r['challenges'] if c['target_agent']==task['agent_id']}
                        draft = capture['model_output']
                        _, preserved = reply_validation(draft.get('replies'), required)
                        repair_context = dict(revision_draft=draft, reply_repair=dict(
                            required_challenge_ids=validation['missing_challenge_ids'],
                            validation_errors=validation['errors'], preserved_replies=preserved))
                        request_context.update(repair_context)
                        checkpoints.write(draft_path, dict(fingerprint=fingerprint, repair_context=repair_context))
                finally:
                    request_count += capture.get('requests', 0)
                    def metrics(s):
                        t = next(t for t in s['rounds'][-1]['tasks'] if t['id']==task['id'])
                        t.update(attempts=request_count, elapsed_seconds=round(time.monotonic()-start, 2),
                                 context_chars=len(json.dumps(request_context, ensure_ascii=False)))
                    self.service._change(self.mid, self.op, metrics, 'task_status')
            card['attempts'] = request_count
            card.update({k: task[k] for k in ('agent_id', 'issue_id', 'phase', 'exchange', 'version') if k in task})
            card.update(id=self.stable_id('proposal', task['id']), predecessor=task.get('predecessor'),
                        evidence_revision=revision(self.evidence), question_version=1)
            if task.get('phase') != 'verification' or complete_verification(card, context):
                checkpoints.write(cache_path, dict(fingerprint=fingerprint, card=card))
            return card
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(execute, t, c): t for t, c in accepted}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    card = future.result()
                    self.normalize_claims(card)
                    self.cards.append(card)
                    self.service._append_card(self.mid, self.op, card)
                    output.append(card)
                except Exception as exc:
                    from .service import error_text
                    message = error_text(exc)
                    self.warnings.append(task.get('agent_id', task['role']) + '：' + message)
                    self.service._task_status(self.mid, self.op, task['id'], 'failed', message,
                                              error_type='response_validation' if isinstance(exc, IncompleteExchange) else '')
        return sorted(output, key=lambda c: c.get('agent_id', ''))

    def normalize_claims(self, card):
        items = card.get('claims')
        if not isinstance(items, list) or not items:
            items = [dict(text=card['conclusion'], kind='inference', evidence_ids=card['evidence_ids'],
                          scope='参见分析局限', action='maintain')]
        claims = []
        for index, item in enumerate(items[:3]):
            if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip():
                continue
            action = item.get('action', 'maintain')
            if action not in ('maintain', 'qualify', 'revise', 'withdraw'):
                action = 'maintain'
            claim = dict(id=self.stable_id('claim', [card['id'], index]), text=item['text'][:2000], kind=item.get('kind', 'inference'),
                         scope=str(item.get('scope', '未说明'))[:500], action=action,
                         evidence_ids=[x for x in item.get('evidence_ids', []) if x in card['evidence_ids']],
                         opposing_evidence_ids=[x for x in item.get('opposing_evidence_ids', []) if x in card['evidence_ids']],
                         evidence_status='unverified', version=card.get('version', 1),
                         author=card.get('agent_id'), previous_id=item.get('previous_id'))
            claims.append(claim)
        if not claims:
            raise RuntimeError('未返回有效主张')
        card['claims'] = claims

    def run(self, tasks):
        self.stage('同题独立作答 · 初稿提交前不交换答案', 'proposing')
        jobs = []
        previous = self.state['snapshots'][-1]['cards'] if self.state['snapshots'] else []
        for index, task in enumerate(tasks):
            # Old interrupted rounds retain their frozen routing. New plans are explicit.
            adaptive = task.get('collaboration_version') in (1, 2)
            paired_role = task.get('collaboration_version') == 2 and task['role'] in ('media', 'sentiment')
            debate = task.get('debate') if adaptive else (task.get('debate') or index == 0)
            reason = task.get('reason') or ('核心议题需要独立提案互审' if debate else '专项材料整理，由核验角色检查')
            has_opinions = self.state['comments'] or any(e.get('source_type') == 'social_post' for e in self.evidence)
            if not paired_role and task['role'] == 'sentiment' and not (has_opinions if adaptive else self.state['comments']) and self.state['mode'] != 'demo':
                debate = False
                reason = '没有评论或平台帖子可供观点分析，本轮仅说明资料缺口并核验'
            if adaptive and not paired_role and not self.evidence and self.state['mode'] != 'demo':
                debate = False
                reason = '尚无可用材料，本轮仅说明资料缺口并核验'
            issue = dict(id=self.stable_id('issue', index), question=task['question'], role=task['role'],
                         feedback_ids=task['feedback_ids'], mode='debate' if debate else 'single',
                         reason=reason, analysis_kind=task.get('analysis_kind'), agent_focus={},
                         require_reciprocal=paired_role,
                         status='proposing', agents=[], exchange=0, stop_reason='')
            self.issues.append(issue)
            common = evidence_context(self.evidence, previous, None, task['question'])
            for j in range(2 if debate else 1):
                agent = f"r{len(self.state['rounds'])}-{task['role']}-{index + 1}-{j + 1}"
                issue['agents'].append(agent)
                instruction = ('独立回答共同问题，不预设立场，不虚构分歧。' +
                               ('优先检查来源归属与证据边界。' if j else '优先梳理事实与论据关系。'))
                if adaptive:
                    focus, guidance = ANALYSIS_FOCUS[task['role']][j]
                    issue['agent_focus'][agent] = focus
                    instruction = '独立回答共同问题，不预设立场，不虚构分歧。' + guidance
                    if task['role'] == 'sentiment':
                        instruction += '共用程序提供的sentiment统计，不重新编造比例；重点比较解释、反例和适用范围。只有帖子时仅作定性分析。'
                        instruction += '没有用户表达材料时，只核查现有说法能否由材料推出及需要什么数据，不生成情绪分布或假设用户立场。'
                t = self.task(task['role'], task['question'], agent_id=agent, issue_id=issue['id'],
                              phase='proposal', version=1, exchange=0, evidence_ids=[e['id'] for e in common],
                              require_reciprocal=paired_role,
                              feedback_ids=task['feedback_ids'], instruction=instruction)
                jobs.append((t, previous))
            # Original planning task is a routing record, not another model invocation.
            self.service._task_status(self.mid, self.op, task['id'], 'complete')
        # Budget a complete exchange and its verification for every selected issue.
        # Three paired issues with two exchanges need at most 51 logical calls.
        exchanges = self.state.get('debate_exchanges', 2)
        self.limit = max(self.limit, sum(len(i['agents']) + (len(i['agents'])*3+1)//2 +
            (len(i['agents'])*3*exchanges if i['mode']=='debate' else 0) for i in self.issues))
        self.save()
        initial = self.batch(jobs)
        if self.stopped():
            return self.cards
        if not initial:
            raise RuntimeError('本轮分析角色均未完成，没有生成讨论快照')
        self.stage('独立答案已冻结，开始交换与互审', 'exchanging')
        self.save('proposal_set_frozen')
        # At most three claims and two challenges per candidate per exchange.
        self.verification_reserves = {i['id']: (sum(c['issue_id']==i['id'] for c in initial)*3 + 1)//2 +
            (len(i['agents'])*exchanges if i['mode']=='debate' else 0) for i in self.issues}
        self.reserve = sum(self.verification_reserves.values())
        for issue in self.issues:
            candidates = [c for c in initial if c['issue_id'] == issue['id']]
            if not candidates:
                issue.update(status='failed', stop_reason='没有有效提案')
                self.reserve -= self.verification_reserves.pop(issue['id'], 0)
                continue
            if issue['mode'] == 'debate' and len(candidates) < 2:
                issue.update(mode='degraded', stop_reason='仅一个有效答案，未完成多实例辩论')
                self.warnings.append(issue['question'] + '：单答案降级')
            if issue['mode'] == 'debate':
                candidates = self.exchange(issue, candidates)
            if self.stopped():
                issue.update(status='unverified', stop_reason='用户要求在阶段边界收束，保留未核验项')
                self.warnings.append(issue['question'] + '：提前收束，核验未完成')
                continue
            self.verify(issue, candidates)
        self.save()
        return self.cards

    def exchange(self, issue, candidates):
        for exchange in range(1, self.state.get('debate_exchanges', 2) + 1):
            if self.exchange_stopped():
                issue['stop_reason'] = '用户结束交锋；仍对已有答案进行核验'
                break
            if self.calls + len(candidates) * 2 + self.reserve > self.limit:
                issue['stop_reason'] = '分析调用额度不足；仍对已有答案进行核验'
                break
            issue['exchange'] = exchange
            self.stage(f'第 {exchange} 次交锋 · 相互评议具体主张', 'challenging')
            jobs = []
            for i, author in enumerate(candidates):
                target = candidates[(i + 1) % len(candidates)]
                task = self.task(author['role'], '评议对方答案：' + issue['question'],
                    agent_id=author['agent_id'], issue_id=issue['id'], phase='peer_review', exchange=exchange,
                    require_reciprocal=issue.get('require_reciprocal', False),
                    allowed_platforms=self.state.get('crawl_platforms', []),
                    instruction='返回review:{agreements:["认可项"],challenges:[{claim_id,question,reason,resolution,search_query}],own_revision:"对自身答案的启发"}。'
                    'claim_id必须是target中的主张ID。最多2项具体质疑；无实质问题返回空数组。不强迫反对。缺证据时search_query提供含事件主体的简短关键词。'
                    '解决条件应要求核对依据、限定范围或撤回，不得要求作者采纳尚无证据支持的更具体指控。'
                    '仅在需要原始平台帖子时为质疑增加platform，必须来自allowed_platforms；否则省略。' +
                    ('首个交锋必须就对方至少一条具体主张提出质询。即便认可结论，也须核查其原文依据、推导、替代解释或适用范围；'
                     '可用中性求证问题，不得伪造反对事实或分歧。后续没有新问题可返回空数组。' if issue.get('require_reciprocal') else ''),
                    feedback_ids=issue['feedback_ids'])
                jobs.append((task, dict(own=author, target=target)))
            reviews = self.batch(jobs)
            records = []
            for card in reviews:
                author_index = next(i for i, c in enumerate(candidates) if c['agent_id'] == card['agent_id'])
                target = candidates[(author_index + 1) % len(candidates)]
                valid = {c['id'] for c in target['claims']}
                data = card.get('review_data')
                data = data if isinstance(data, dict) else {}
                challenges = []
                raw = data.get('challenges')
                complete = isinstance(raw, list)
                for c in (raw if complete else [])[:2]:
                    if isinstance(c, dict) and c.get('claim_id') in valid and all(isinstance(c.get(k), str) and c[k].strip() for k in ('question', 'reason', 'resolution')):
                        challenges.append(dict(id=self.stable_id('challenge', [card['id'], len(challenges)]), claim_id=c['claim_id'], target_agent=target['agent_id'],
                            question=c['question'][:1000], reason=c['reason'][:1000], resolution=c['resolution'][:1000],
                            search_query=str(c.get('search_query', ''))[:160], status='open',
                            platform=c.get('platform') if c.get('platform') in self.state.get('crawl_platforms', []) else None))
                    else:
                        complete = False
                record = dict(id=self.stable_id('peer-review', card['id']), issue_id=issue['id'], exchange=exchange,
                              author=card['agent_id'], target=target['id'], target_agent=target['agent_id'],
                              agreements=data.get('agreements', []), challenges=challenges,
                              own_revision=str(data.get('own_revision', ''))[:2000], complete=complete)
                records.append(record)
            self.reviews.extend(records)
            self.save('peer_review_posted')
            if len(records) != len(candidates) or not all(r['complete'] for r in records):
                self.warnings.append(issue['question'] + '：互审覆盖不完整，不能视为已达成共识')
            challenges = [c for r in records for c in r['challenges']]
            if challenges and not self.exchange_stopped():
                self.supplement(issue, challenges)
            if self.stopped():
                issue['stop_reason'] = '用户收束，质疑尚待回应'
                break
            self.stage('各实例分别修订 · 保留初稿与修订依据', 'revising')
            jobs = []
            # Carry unanswered earlier questions forward; a quiet later exchange
            # must not let an author bypass a failed response from the first one.
            revision_reviews = [r for r in self.reviews if r['issue_id'] == issue['id']]
            for author in candidates:
                pending = []
                for review in revision_reviews:
                    questions = [c for c in review['challenges'] if c['target_agent'] == author['agent_id']
                                 and not (c.get('revised_proposal') and c.get('status') in ('responded', 'resolved')
                                          and valid_reply(c.get('reply'), c['id']))]
                    if questions:
                        pending.append(dict(review, challenges=questions))
                context = dict(own=author, frozen_candidates=candidates, peer_reviews=pending,
                               supplement_results=[t for t in self.tools if t['issue_id']==issue['id']])
                task = self.task(author['role'], issue['question'], agent_id=author['agent_id'], issue_id=issue['id'],
                    require_reciprocal=issue.get('require_reciprocal', False),
                    phase='revision', version=author['version'] + 1, exchange=exchange, predecessor=author['id'],
                    feedback_ids=issue['feedback_ids'],
                    instruction='根据冻结的互审及新增证据独立修订完整答案。逐条注明维持、限定、修订或撤回及理由。不能看其他实例正在生成的修订。'
                    '返回replies:[{challenge_id,action,response,evidence_ids}]，逐项回应peer_reviews中的待答质疑，每个ID恰好一次。已答的历史质疑无需重复，历史记录仍会核验。action限maintain/qualify/revise/withdraw。'
                    '保持原主张previous_id。结合supplement_results的本地候选原文和新增材料回应，候选仅表示检索匹配，不等于支持。'
                    '区分未执行检索、重复材料、筛选排除及真正无结果，不把预算耗尽说成搜不到。无新证据仍可依原文限定或撤回，认可意见不等于核实。')
                jobs.append((task, context))
            revised = self.batch(jobs)
            for card in revised:
                own_old = next(c for c in candidates if c['agent_id'] == card['agent_id'])
                old_ids = {c['id'] for c in own_old['claims']}
                for c in card['claims']:
                    if c['previous_id'] not in old_ids:
                        c['previous_id'] = None
                replies = card.get('reply_data') if isinstance(card.get('reply_data'), list) else []
                for challenge in (c for r in revision_reviews for c in r['challenges']):
                    reply = next((r for r in replies if isinstance(r, dict) and r.get('challenge_id') == challenge['id'] and r.get('response')), None)
                    if reply and challenge['target_agent'] == card['agent_id']:
                        challenge.update(status='responded', reply=reply, revised_proposal=card['id'])
            self.save('proposal_revised')
            if len(revised) < len(candidates):
                self.warnings.append(issue['question'] + '：部分修订失败，保留原版本及未解决质疑')
            latest = {c['agent_id']: c for c in candidates}
            latest.update({c['agent_id']: c for c in revised})
            candidates = list(latest.values())
            if not challenges:
                issue['stop_reason'] = '本回合没有有效新增质疑；仍由核验检查共同依据'
                break
            if exchange == self.state.get('debate_exchanges', 2):
                issue['stop_reason'] = '到达交锋上限，剩余争议交还用户'
        return candidates

    def supplement(self, issue, challenges):
        path = self.folder / ('supplement-' + checkpoints.digest([issue['id'], challenges])[:16] + '.json')
        cached = checkpoints.read(path)
        if cached:
            seen = {e['id'] for e in self.evidence}
            self.evidence.extend(e for e in cached['added'] if e['id'] not in seen)
            self.tools.extend(cached['tools'])
            self.sentiment = cached['sentiment']
            self.state['comments'] = cached['comments']
            collector = self.service.collectors[self.op]
            collector.used = max(collector.used, cached.get('collection_used', 0))
            collector.request_count = max(collector.request_count, cached.get('request_count', 0))
            return
        before = {e['id'] for e in self.evidence}
        tools_before = len(self.tools)
        self._supplement(issue, challenges)
        checkpoints.write(path, dict(added=[e for e in self.evidence if e['id'] not in before],
            tools=self.tools[tools_before:], sentiment=self.sentiment, comments=self.state['comments'],
            collection_used=self.service.collectors[self.op].used,
            request_count=self.service.collectors[self.op].request_count))

    def _supplement(self, issue, challenges):
        self.stage('按质疑补充证据 · 同批完成后再修订', 'supplementing')
        from .service import error_text
        for challenge in challenges:
            query = challenge['search_query']
            if self.state['mode']=='live' and not self.stopped():
                from .vision import select_posts
                parents = select_posts(self.evidence, [query or challenge['question']])
                added = self.service._analyze_image_evidence(self.state, self.op, self.evidence, self.warnings, parents)
                if added:
                    self.tools.append(dict(id=uid('tool'), issue_id=issue['id'], challenge_id=challenge['id'],
                        query=query, status='complete', kind='image_analysis', new_evidence_ids=[e['id'] for e in added],
                        message='已读取相关原图并提取文字与可见信息；同批修订实例使用一致的图片证据'))
            if query and challenge.get('platform') and self.state['mode'] == 'live' and not self.stopped():
                self.supplement_platform(issue, challenge)
            if not query or self.stopped():
                continue
            record = dict(id=uid('tool'), issue_id=issue['id'], challenge_id=challenge['id'], query=query,
                          status='local', new_evidence_ids=[], message='先检索会议证据库')
            local = retrieve(query, self.evidence, 5)
            record['local_evidence_ids'] = [e['id'] for e in local]
            previous = next((t for t in self.tools if t['query']==query and not t.get('platform')), None)
            if query in self.searches and previous:
                record.update(status='reused', source_tool_id=previous['id'],
                              message='复用本轮相同查询结果：' + previous['message'],
                              new_evidence_ids=previous['new_evidence_ids'], request_count=0)
            elif self.state['mode'] == 'demo':
                record.update(status='demo', message='虚构演示：从预置资料补充，不调用网络')
            elif not self.state['web_enabled']:
                record.update(status='unavailable', message='联网未启用，使用现有材料；不足处保留')
            else:
                self.searches.add(query)
                try:
                    seen = {e['id'] for e in self.evidence}
                    found = self.service._search(query, self.state, self.warnings,
                        question=challenge['question'], condition=challenge['resolution'])
                    added = [e for e in found if e['id'] not in seen]
                    present = {e['id'] for e in self.evidence}
                    self.evidence.extend(e for e in added if e['id'] not in present)
                    def persist(s):
                        seen_stored = {e['id'] for e in s['evidence']}
                        s['evidence'].extend(e for e in added if e['id'] not in seen_stored)
                    self.service._change(self.mid, self.op, persist, 'evidence')
                    from .collection import supplement_outcome
                    collector = self.service.collectors[self.op]
                    task = next(t for t in reversed(collector.records) if t.get('kind')=='web' and t['query']==query)
                    record.update(supplement_outcome(task, [e['id'] for e in added], collector.records),
                                  new_evidence_ids=[e['id'] for e in added])
                except Exception as exc:
                    record.update(status='failed', message=error_text(exc))
                    self.warnings.append('质疑补证失败：' + error_text(exc))
            self.tools.append(record)
            self.save('evidence_supplemented')

    def supplement_platform(self, issue, challenge):
        collector = self.service.collectors[self.op]
        platform, query = challenge['platform'], challenge['search_query']
        if platform not in self.state.get('crawl_platforms', []):
            return
        key = (platform, query, challenge['question'])
        if key in self.platform_searches:
            return
        self.platform_searches.add(key)
        before = {e['id'] for e in self.evidence}
        record = dict(id=uid('tool'), issue_id=issue['id'], challenge_id=challenge['id'], query=query,
                      platform=platform, status='running', new_evidence_ids=[], message='按质疑分页补证')
        self.tools.append(record)
        self.save('evidence_supplemented')
        if collector.stopped():
            record.update(status='user_stopped', request_count=0,
                          message='未发起平台补证：用户已停止采集')
        else:
            records = collector.run(plans=[dict(platform=platform, query=query, question=challenge['question'],
                condition=challenge['resolution'], reason='回应辩论中的具体质疑', comments=self.state['report_type']=='opinion' or '评论' in challenge['question'])])
            from .vision import select_posts
            self.service._analyze_image_evidence(self.state, self.op, self.evidence, self.warnings,
                select_posts(self.evidence, [query, challenge['question']]))
            added = [e['id'] for e in self.evidence if e['id'] not in before]
            record.update(status='complete' if added else 'partial', new_evidence_ids=added,
                          message='；'.join(dict.fromkeys(r.get('message', '') for r in records)), scope='协调模型按问题覆盖决定继续、调整或停止')
            if self.state['comments']:
                self.sentiment = self.service._sentiment(self.state['comments'], self.state)
                self.service._change(self.mid, self.op, lambda s: s['rounds'][-1].update(sentiment=self.sentiment), 'sentiment_updated')
        self.save('evidence_supplemented')

    def verify(self, issue, candidates):
        self.stage('核验修订与共同依据 · 不采用多数投票', 'verifying')
        reviews = [r for r in self.reviews if r['issue_id'] == issue['id']]
        claims = {c['id']: c for p in candidates for c in p['claims']}
        compact = [dict(agent_id=p['agent_id'], claims=p['claims']) for p in candidates]
        jobs = []
        def job(group, peer_reviews, required):
            task = self.task('review', '分批核验：' + issue['question'],
                agent_id='verifier-' + issue['id'] + '-' + str(len(jobs)+1), issue_id=issue['id'], phase='verification',
                **({'strict_verification': True} if self.state['rounds'][-1].get('remediation') else {}),
                instruction='返回verifications:[{claim_id,status,reason,evidence_ids}]，只核验required_claim_ids，每个ID恰好一项。'
                'status限supported/partial/insufficient/conflicting/refuted；reason解释原文与主张关系，缺材料不能判supported。'
                '返回replies:[{challenge_id,resolved,reason,evidence_ids}]，只处理提供的质疑，resolved表示解决条件已满足。'
                '结论控制在120字内，每项reason控制在100字内。相同模型达成一致不等于事实。'
                '帖子及媒体转述不等于一级评论样本；评论数量只能以sample_scope为准，不能以帖子存在反驳无评论样本。'
                '原始来源身份必须核对页面出处，媒体转述不能标为官方原始发布。核对完整年份及归因，不可只因月日吻合判完全支持。'
                '没有检索到回应只能说明本证据集中未发现，不能证明当事人从未回应。')
            jobs.append((task, dict(candidates=group, peer_reviews=peer_reviews, required_claim_ids=required,
                sample_scope=dict(comment_count=len(self.state['comments']),
                                  social_post_count=sum(e.get('source_type')=='social_post' for e in self.evidence)))))
        items = list(claims.values())
        for start in range(0, len(items), 2):
            ids = {c['id'] for c in items[start:start+2]}
            group = [dict(agent_id=p['agent_id'], claims=[c for c in p['claims'] if c['id'] in ids]) for p in compact]
            job([p for p in group if p['claims']], [], sorted(ids))
        challenges = [c for r in reviews for c in r['challenges']]
        for start in range(0, len(challenges), 2):
            batch = challenges[start:start+2]
            agents = {c['target_agent'] for c in batch}
            job([p for p in compact if p['agent_id'] in agents], [dict(challenges=batch)], [])
        self.reserve = max(0, self.reserve - self.verification_reserves.pop(issue['id'], 0))
        results = self.batch(jobs)
        expected = {t['id']: set(ctx['required_claim_ids']) for t, ctx in jobs}
        checks = [v for r in results for v in (r.get('verification_data') if isinstance(r.get('verification_data'), list) else [])
                  if isinstance(v, dict) and v.get('claim_id') in expected[r['task_id']]]
        seen = set()
        for check in checks if isinstance(checks, list) else []:
            if not isinstance(check, dict) or check.get('claim_id') not in claims or check['claim_id'] in seen:
                continue
            status = check.get('status')
            if status not in ('supported', 'partial', 'insufficient', 'conflicting', 'refuted') or not check.get('reason'):
                continue
            if status == 'supported' and not check.get('evidence_ids'):
                status = 'insufficient'
            seen.add(check['claim_id'])
            claims[check['claim_id']]['evidence_status'] = status
            claims[check['claim_id']]['verification_reason'] = check['reason']
            self.verifications.append(dict(check, status=status, issue_id=issue['id'], method='模型语义核验，非人工确认'))
        replies = [v for r in results for v in (r.get('reply_data') if isinstance(r.get('reply_data'), list) else [])]
        for review in reviews:
            for challenge in review['challenges']:
                check = next((r for r in replies if isinstance(r, dict) and r.get('challenge_id') == challenge['id'] and r.get('reason')), None) if isinstance(replies, list) else None
                if check and check.get('resolved') is True and challenge['status'] == 'responded':
                    challenge.update(status='resolved', verification=check)
                elif check and isinstance(check.get('resolved'), bool):
                    challenge['verification'] = check
        gaps = [c for r in reviews for c in r['challenges'] if c['status'] != 'resolved']
        incomplete = len(seen) < len(claims) or any(not r['complete'] for r in reviews)
        incomplete = incomplete or ((issue['mode'] == 'debate' or issue.get('require_reciprocal'))
                                    and len({r['author'] for r in reviews}) < 2)
        evidence_gap = any(c['evidence_status'] != 'supported' for c in claims.values() if c['action'] != 'withdraw')
        issue['status'] = 'unresolved' if gaps or incomplete or evidence_gap else 'reviewed'
        if not issue['stop_reason']:
            issue['stop_reason'] = '专项分析及核验完成' if issue['status'] == 'reviewed' else '资料或核验仍有缺口，等待用户决定'
        if incomplete or gaps or evidence_gap:
            reasons = []
            if gaps:
                reasons.append('尚有未解决质疑')
            if incomplete:
                reasons.append('互审或核验未完整完成')
            if evidence_gap:
                reasons.append('部分主张证据支持有限')
            self.warnings.append(issue['question'] + '：' + '；'.join(reasons) + '，报告须保留局限')
        # Cards in the database were appended before verification. Sync only status metadata.
        statuses = {cid: claim['evidence_status'] for cid, claim in claims.items()}
        def persist(s):
            for card in s['rounds'][-1]['cards']:
                for claim in card.get('claims', []):
                    if claim['id'] in statuses:
                        claim['evidence_status'] = statuses[claim['id']]
                        claim['verification_reason'] = claims[claim['id']].get('verification_reason', '')
        self.service._change(self.mid, self.op, persist, 'verified')
        self.save()
