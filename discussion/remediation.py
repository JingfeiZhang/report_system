"""Complete rejected revisions in a new version without rewriting frozen history."""
from __future__ import annotations

import copy
import json
import re

from . import checkpoints
from .debate import DebateRunner, revision, valid_reply
from .store import Conflict, now, uid


def start(service, mid, data):
    def prepare(s):
        if data.get('expected_round') != len(s['rounds']):
            raise Conflict('会议已更新，请刷新后再补救')
        if not s['rounds'] or s['rounds'][-1]['status'] != 'complete':
            raise Conflict('未冻结的失败轮次请使用恢复未完成阶段')
        source = s['rounds'][-1]
        snapshot = next((x for x in s['snapshots'] if x['round'] == source['number']), None)
        failed = [t for t in source['tasks'] if t.get('phase') == 'revision'
                  and t['status'] in ('failed', 'interrupted', 'cancelled')]
        # A later accepted revision already supersedes an earlier failed attempt.
        failed = [t for t in failed if not any(c.get('agent_id') == t.get('agent_id')
                  and c.get('phase') == 'revision' and c.get('version', 1) >= t.get('version', 1)
                  for c in source['cards'])]
        if not snapshot or not failed:
            raise Conflict('最新已冻结轮次没有待补救的失败修订')
        if len({t['agent_id'] for t in failed}) != len(failed):
            raise Conflict('同一实例存在多阶段失败，请在下一轮重新安排该议题')
        affected = {t['issue_id'] for t in failed}
        cards = copy.deepcopy([c for c in source['cards'] if c.get('issue_id') in affected
                               and c.get('phase') in ('proposal', 'revision')])
        issues = copy.deepcopy([i for i in source['issues'] if i['id'] in affected])
        reviews = copy.deepcopy([r for r in source.get('peer_reviews', []) if r['issue_id'] in affected])
        tools = copy.deepcopy([t for t in source.get('tool_runs', []) if t['issue_id'] in affected])
        for item in cards + reviews + tools:
            item['carried_from_round'] = source['number']
        for issue in issues:
            issue.update(status='revising', stop_reason='')
        for review in reviews:
            for challenge in review['challenges']:
                challenge.pop('verification', None)
                answered = valid_reply(challenge.get('reply'), challenge['id']) and challenge.get('revised_proposal')
                challenge['status'] = 'responded' if answered else 'open'
        s['rounds'].append(dict(number=len(s['rounds'])+1, status='running', created_at=now(),
            stop_requested=False, collection_stop_requested=True, tasks=[], cards=cards,
            issues=issues, peer_reviews=reviews, tool_runs=tools, verifications=[], warnings=[],
            summary=None, sentiment=copy.deepcopy(snapshot.get('sentiment')), stage='正在补救失败修订',
            remediation=dict(source_round=source['number'], source_snapshot_id=snapshot['id'],
                source_task_ids=[t['id'] for t in failed], status='running', results=[])))
    state, op = service._claim(mid, 'running', prepare)
    frozen = copy.deepcopy(state)
    meta = frozen['rounds'][-1]['remediation']
    snapshot = next(x for x in frozen['snapshots'] if x['id'] == meta['source_snapshot_id'])
    frozen['evidence'] = copy.deepcopy(snapshot['evidence'])
    # Use the original sample, never today's comments with yesterday's percentages.
    original = checkpoints.read(service.store.root / mid / f"round-{meta['source_round']}" / 'prepared.json')
    if original is not None:
        frozen['comments'] = original['comments']
    else:
        ids = {e['id'] for e in frozen['evidence']}
        frozen['comments'] = [c for c in frozen['comments'] if c.get('evidence_id') in ids]
    checkpoints.write(service.round_folder(frozen) / 'input.json', frozen)
    service.pool.submit(run, service, frozen, op)
    return state


def revision_jobs(runner, original, task_ids):
    jobs = []
    for failed in original['tasks']:
        if failed['id'] not in task_ids:
            continue
        candidates = {}
        for card in original['cards']:
            if (card.get('issue_id') == failed['issue_id'] and card.get('phase') in ('proposal', 'revision')
                    and card.get('exchange', 0) < failed['exchange']):
                agent = card['agent_id']
                if agent not in candidates or card.get('version', 1) > candidates[agent].get('version', 1):
                    candidates[agent] = card
        own = candidates[failed['agent_id']]
        pending = []
        for review in original['peer_reviews']:
            if review['issue_id'] != failed['issue_id'] or review['exchange'] > failed['exchange']:
                continue
            questions = [c for c in review['challenges'] if c['target_agent'] == own['agent_id']
                         and not (c.get('revised_proposal') and c.get('status') in ('responded', 'resolved')
                                  and valid_reply(c.get('reply'), c['id']))]
            if questions:
                pending.append(dict(review, challenges=questions))
        task = runner.task(failed['role'], failed['question'],
            **{k: failed[k] for k in ('agent_id', 'issue_id', 'feedback_ids', 'exchange', 'version', 'predecessor')},
            phase='revision', require_reciprocal=True, source_task_id=failed['id'],
            instruction='依据已冻结的答案和证据修订，逐项回应peer_reviews中的待答问题。'
            '返回replies:[{challenge_id,action,response,evidence_ids}]，每个ID恰好一次；已答旧问题无需重复。'
            '保持主张previous_id。action限maintain/qualify/revise/withdraw。'
            '未取得新增材料时据原文作答、限定或撤回，不得声称本次已重新采集。')
        jobs.append((task, dict(own=own, frozen_candidates=list(candidates.values()), peer_reviews=pending,
                               supplement_results=[t for t in original.get('tool_runs', [])
                                                   if t['issue_id'] == failed['issue_id']])))
    return jobs


def run(service, state, op):
    mid, n = state['id'], len(state['rounds'])
    folder = service.round_folder(state)
    meta = state['rounds'][-1]['remediation']
    original = next(r for r in state['rounds'] if r['number'] == meta['source_round'])
    snapshot = next(s for s in state['snapshots'] if s['id'] == meta['source_snapshot_id'])
    evidence, warnings = state['evidence'], []
    runner = DebateRunner(service, state, op, evidence, snapshot.get('sentiment'), warnings)
    runner.issues = copy.deepcopy(state['rounds'][-1]['issues'])
    runner.reviews = copy.deepcopy(state['rounds'][-1]['peer_reviews'])
    runner.tools = copy.deepcopy(state['rounds'][-1]['tool_runs'])
    try:
        if service._cancel_round_if_requested(mid, op):
            return
        runner.stage('补救原交锋的失败修订 · 沿用冻结资料', 'revising')
        jobs = revision_jobs(runner, original, meta['source_task_ids'])
        results = runner.batch(jobs)
        for card in results:
            for review in runner.reviews:
                for challenge in review['challenges']:
                    reply = next((x for x in card.get('reply_data', []) if x['challenge_id'] == challenge['id']
                                  and challenge['target_agent'] == card['agent_id']), None)
                    if reply:
                        challenge.update(status='responded', reply=reply, revised_proposal=card['id'])
        runner.save()
        if service._cancel_round_if_requested(mid, op):
            return
        if len(results) != len(jobs):
            raise RuntimeError('补救修订尚未全部通过；请恢复未完成阶段，已成功的修订会按相同输入复用')
        current = {c.get('agent_id', c['role']): copy.deepcopy(c) for c in snapshot['cards']
                   if c.get('phase') in ('proposal', 'revision')}
        current.update({c['agent_id']: c for c in results})
        for issue in runner.issues:
            if service._cancel_round_if_requested(mid, op):
                return
            runner.verify(issue, [c for c in current.values() if c.get('issue_id') == issue['id']])
        verification = checkpoints.verification_summary(list(current.values()))
        live = service.store.get(mid)['rounds'][-1]
        if any(t['status'] != 'complete' for t in live['tasks']) or verification['pending']:
            raise RuntimeError('补救核验尚未全部完成；请恢复未完成阶段，成功批次会复用')
        if service._cancel_round_if_requested(mid, op):
            return
        runner.stage('主持人汇总补救结果', 'summarizing')
        from .service import strings
        payload = dict(cards=list(current.values()), issues=runner.issues, peer_reviews=runner.reviews,
                       verifications=runner.verifications, sentiment=snapshot.get('sentiment'),
                       feedback=snapshot.get('feedback', []), warnings=warnings,
                       remediation=dict(source_round=meta['source_round'], scope='补跑失败修订和核验；本次没有重新采集'))
        identity = service.provider.call_identity('host') if hasattr(service.provider, 'call_identity') else type(service.provider).__name__
        fingerprint = checkpoints.digest([payload, identity, 'remediation-summary-v1'])
        cached = checkpoints.read(folder / 'remediation-summary.json')
        if cached and cached['fingerprint'] == fingerprint:
            summary = cached['summary']
        else:
            summary = service.provider.json('host',
                '汇总研讨。返回 {"overview":"简明纪要","agreements":["共识"],"disagreements":["分歧"],'
                '"gaps":["证据或任务缺口"],"next_questions":["下一轮建议"]}。'
                '这是对已有轮次失败修订的补救；只使用最新答案及实际核验结果，不重复旧技术失败。'
                '说明哪些观点维持或修订及原因，未解决质疑保留。证据不足是核验结论，不等于任务未执行。'
                '未核验、部分支持不得升级为确定事实；模型一致不等于事实证明。评论比例沿用程序统计。', payload)
            if not isinstance(summary.get('overview'), str) or not summary['overview'].strip():
                raise RuntimeError('补救主持汇总不完整，请恢复未完成阶段')
            summary = dict(overview=summary['overview'][:5000], **{k: strings(summary.get(k))
                for k in ('agreements', 'disagreements', 'gaps', 'next_questions')})
            if not set(re.findall(r'E-[a-f0-9]{12}', json.dumps(summary))) <= {e['id'] for e in evidence}:
                raise RuntimeError('补救汇总包含未知引用，结果未发布')
            checkpoints.write(folder / 'remediation-summary.json', dict(fingerprint=fingerprint, summary=summary))
        if service._cancel_round_if_requested(mid, op):
            return
        mapping = [dict(source_task_id=t['source_task_id'], task_id=t['id'],
                        card_id=next(c['id'] for c in results if c['task_id'] == t['id'])) for t, _ in jobs]
        finished = dict(meta, status='complete', results=mapping, completed_at=now())
        saved = dict(id=uid('snapshot'), round=n, created_at=now(), schema_version=2,
            cards=list(current.values()), summary=summary, evidence=evidence, sentiment=snapshot.get('sentiment'),
            agenda=snapshot['agenda'], feedback=copy.deepcopy(snapshot.get('feedback', [])), warnings=warnings,
            collections=copy.deepcopy(snapshot.get('collections', [])), issues=copy.deepcopy(runner.issues),
            proposals=copy.deepcopy(service.store.get(mid)['rounds'][-1]['cards']),
            peer_reviews=copy.deepcopy(runner.reviews), verifications=copy.deepcopy(runner.verifications),
            tool_runs=copy.deepcopy(runner.tools), evidence_revision=revision(evidence),
            verification_summary=verification, report_ready=True, remediation=finished,
            claims=[copy.deepcopy(c) for p in current.values() for c in p.get('claims', [])])
        def complete(s):
            s['rounds'][-1].update(status='complete', stage='补救完成，等待用户决策', summary=summary,
                warnings=warnings, remediation=finished, verification_summary=verification, report_ready=True)
            s['snapshots'].append(saved)
            s.update(status='waiting', operation=None, error='')
        service._change(mid, op, complete, 'round_complete')
    except Exception as exc:
        service._failure(mid, op, exc)
