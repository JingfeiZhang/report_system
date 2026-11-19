"""Regression tests for pending questions and targeted revision repair (no API calls)."""
import copy
import json
import unittest
from pathlib import Path

import test_discussion_debate as fixtures


class RevisionRepairTests(unittest.TestCase):
    setUp = fixtures.DebateTests.setUp
    tearDown = fixtures.DebateTests.tearDown
    state = fixtures.DebateTests.state
    run_round = fixtures.DebateTests.run_round
    plan_tasks = fixtures.DebateTests.plan_tasks

    def paired(self):
        self.plan_tasks([dict(role='media', question='核查媒体观点')])
        return self.provider.json

    def test_second_revision_accepts_new_answers_without_repeating_answered_questions(self):
        original = self.paired()
        def provider(role, instruction, data):
            result = original(role, instruction, data)
            task = data.get('task', {})
            if task.get('phase') == 'peer_review':
                result['review']['challenges'] = [dict(claim_id=data['previous']['target']['claims'][0]['id'],
                    question='请核对本次表述范围', reason='需要核查', resolution='限定范围', search_query='')]
            if task.get('phase') == 'revision' and task['exchange'] == 2:
                fresh = {c['id'] for r in data['previous']['peer_reviews'] if r['exchange'] == 2 for c in r['challenges']}
                result['replies'] = [r for r in result['replies'] if r['challenge_id'] in fresh]
            return result
        self.provider.json = provider
        state = self.run_round()
        failed = [t for t in state['rounds'][0]['tasks'] if t.get('phase') == 'revision' and t['status'] != 'complete']
        self.assertEqual(failed, [], 'Already answered questions must not fail the second revision')
        reviews = state['rounds'][0]['peer_reviews']
        self.assertEqual(len(reviews), 4)
        self.assertTrue(all(c.get('reply') and c['status'] == 'resolved' for r in reviews for c in r['challenges']))

    def test_retry_requests_only_missing_replies_and_keeps_valid_draft(self):
        original = self.paired()
        first_drafts, repairs = {}, []
        def provider(role, instruction, data):
            result = original(role, instruction, data)
            task, context = data.get('task', {}), data.get('previous', {})
            if task.get('phase') == 'peer_review' and task['exchange'] == 1:
                result['review']['challenges'] *= 2
            if task.get('phase') == 'revision':
                if context.get('reply_repair'):
                    repairs.append(copy.deepcopy(data))
                    requested = set(context['reply_repair']['required_challenge_ids'])
                    return {'replies': [r for r in result['replies'] if r['challenge_id'] in requested]}
                result['conclusion'] = '保留这份已生成的修订正文'
                result['replies'] = result['replies'][:1]
                first_drafts[task['id']] = copy.deepcopy(result)
            return result
        self.provider.json = provider
        state = self.run_round()
        self.assertEqual(len(repairs), 2)
        for request in repairs:
            spec = request['previous']['reply_repair']
            first = first_drafts[request['task']['id']]
            self.assertEqual(len(spec['required_challenge_ids']), 1)
            self.assertNotIn(first['replies'][0]['challenge_id'], spec['required_challenge_ids'])
            self.assertTrue(spec['validation_errors'])
            card = next(c for c in state['rounds'][0]['cards'] if c['task_id'] == request['task']['id'])
            self.assertEqual(card['conclusion'], first['conclusion'])
            self.assertEqual(len(card['reply_data']), 2)
            self.assertIn(first['replies'][0], card['reply_data'])
        self.assertFalse(any(t['status'] == 'failed' for t in state['rounds'][0]['tasks']))
        attempts = list((Path(self.tmp.name) / self.mid / 'round-1' / 'failures').glob('*.json'))
        self.assertEqual(len(attempts), 2)
        for path in attempts:
            record = json.loads(path.read_text(encoding='utf-8'))
            self.assertIn('replies', record['model_output'])
            self.assertEqual(len(record['validation']['missing_challenge_ids']), 1)

    def test_unanswered_earlier_questions_remain_required(self):
        original = self.paired()
        second_requirements = []
        def provider(role, instruction, data):
            result = original(role, instruction, data)
            task = data.get('task', {})
            if task.get('phase') == 'revision':
                if task['exchange'] == 1:
                    result['replies'] = []
                else:
                    second_requirements.append([c['id'] for r in data['previous']['peer_reviews'] for c in r['challenges']
                                                if c['target_agent'] == task['agent_id']])
            return result
        self.provider.json = provider
        state = self.run_round()
        self.assertEqual(len(second_requirements), 2)
        self.assertTrue(all(second_requirements))
        self.assertTrue(all(c.get('reply') and c['status'] == 'resolved' for r in state['rounds'][0]['peer_reviews'] for c in r['challenges']))

    def test_failed_repair_does_not_publish_partial_revision(self):
        original = self.paired()
        def provider(role, instruction, data):
            result = original(role, instruction, data)
            if data.get('task', {}).get('phase') == 'revision':
                result['replies'] = []
            return result
        self.provider.json = provider
        state = self.run_round()
        self.assertFalse(any(c['phase'] == 'revision' for c in state['rounds'][0]['cards']))
        failed = [t for t in state['rounds'][0]['tasks'] if t.get('phase') == 'revision']
        self.assertTrue(failed)
        self.assertTrue(all(t['status'] == 'failed' and t['error_type'] == 'response_validation' for t in failed))
        self.assertTrue(all(c['status'] == 'open' and not c.get('reply') for r in state['rounds'][0]['peer_reviews'] for c in r['challenges']))

    def test_reply_validation_rejects_duplicates_invalid_actions_and_unknown_ids(self):
        from discussion.debate import reply_validation
        good = dict(challenge_id='c1', action='qualify', response='限定范围')
        for invalid in ([good, good], [dict(good, action='agree')], [dict(good, response=' ')],
                        [dict(good, challenge_id=[])], [good, dict(good, challenge_id='unknown')]):
            with self.subTest(replies=invalid):
                validation, _ = reply_validation(invalid, {'c1'})
                self.assertTrue(validation['errors'])

    def test_reply_repair_cannot_introduce_unknown_evidence(self):
        original = self.paired()
        def provider(role, instruction, data):
            result = original(role, instruction, data)
            if data.get('task', {}).get('phase') == 'revision':
                if data['previous'].get('reply_repair'):
                    for reply in result['replies']:
                        reply['evidence_ids'] = ['E-000000000000']
                else:
                    result['replies'] = []
            return result
        self.provider.json = provider
        state = self.run_round()
        self.assertFalse(any(c['phase'] == 'revision' for c in state['rounds'][0]['cards']))
        self.assertTrue(any('未知引用' in t.get('error', '') for t in state['rounds'][0]['tasks']))

    def test_individual_repairs_resume_only_missing_answers_without_regenerating_draft(self):
        original = self.paired()
        self.service.store.change(self.mid, lambda s: s.update(debate_exchanges=1))
        failing = True
        drafts, requests, failed_ids = [], [], {}
        def provider(role, instruction, data):
            if '"overview"' in instruction and failing:
                raise RuntimeError('测试汇总中断，保留未冻结轮次')
            result = original(role, instruction, data)
            task, context = data.get('task', {}), data.get('previous', {})
            if task.get('phase') == 'peer_review':
                result['review']['challenges'] *= 2
            if task.get('phase') == 'revision':
                if context.get('reply_repair'):
                    ids = context['reply_repair']['required_challenge_ids']
                    self.assertEqual(len(ids), 1)
                    self.assertEqual([x['challenge_id'] for x in data['reply_requirements']], ids)
                    self.assertEqual([c['id'] for r in context['peer_reviews'] for c in r['challenges']], ids)
                    requests.append((task['id'], ids[0]))
                    failed_ids.setdefault(task['id'], ids[0])
                    if failing and failed_ids[task['id']] == ids[0]:
                        return {'replies': []}
                    return {'replies': result['replies']}
                drafts.append(task['id'])
                result['replies'] = []
            return result
        self.provider.json = provider
        self.service.start_round(self.mid, {'expected_round': 0})
        first = self.state()
        self.assertEqual(first['status'], 'error')
        self.assertEqual(first['rounds'][0]['status'], 'failed')
        revisions = [t for t in first['rounds'][0]['tasks'] if t.get('phase') == 'revision']
        self.assertEqual(len(revisions), 2)
        for task in revisions:
            self.assertEqual(task['reply_repair']['completed'], 1)
            self.assertEqual(len(task['reply_repair']['pending']), 1)
            self.assertEqual(task['attempts'], 3)  # Draft + two individual repairs.
            self.assertTrue(task['response_validation']['errors'])
        self.assertFalse(any(c['phase']=='revision' for c in first['rounds'][0]['cards']))
        before = len(requests)
        failing = False
        self.service.resume_round(self.mid)
        state = self.state()
        self.assertEqual(state['status'], 'waiting', state['error'])
        self.assertEqual(len(drafts), 2, 'Recovery must not rewrite valid drafts')
        self.assertEqual(set(requests[before:]), set(failed_ids.items()))
        self.assertEqual(len(requests[before:]), 2, 'Successful individual repairs must be reused')
        self.assertTrue(all(t['status']=='complete' for t in state['rounds'][0]['tasks']))
        self.assertTrue(all(c.get('reply') for r in state['rounds'][0]['peer_reviews'] for c in r['challenges']))

    def test_media_and_sentiment_finish_two_exchanges_with_single_question_repairs(self):
        self.plan_tasks([dict(role=role, question='核查'+role) for role in ('media','sentiment')])
        original = self.provider.json
        repairs = []
        def provider(role, instruction, data):
            result = original(role, instruction, data)
            task, context = data.get('task', {}), data.get('previous', {})
            if task.get('phase')=='peer_review':
                result['review']['challenges'] = [dict(claim_id=context['target']['claims'][0]['id'],
                    question='核查范围'+str(i), reason='范围需要明确', resolution='限定范围', search_query='') for i in range(2)]
            if task.get('phase')=='revision':
                if context.get('reply_repair'):
                    repairs.append(context['reply_repair']['required_challenge_ids'])
                    return {'replies': result['replies']}
                result['replies'] = []
            return result
        self.provider.json = provider
        state = self.run_round()
        self.assertEqual(len(repairs), 16)
        self.assertTrue(all(len(ids)==1 for ids in repairs))
        revisions = [c for c in state['rounds'][0]['cards'] if c['phase']=='revision']
        self.assertEqual(len(revisions), 8)
        self.assertTrue(all(len(c['reply_data'])==2 for c in revisions))
        self.assertFalse(any(t['status']=='failed' for t in state['rounds'][0]['tasks']))

    def test_repair_timeout_remains_request_failure_not_response_validation(self):
        original = self.paired()
        self.service.store.change(self.mid, lambda s:s.update(debate_exchanges=1))
        def provider(role, instruction, data):
            result = original(role, instruction, data)
            if data.get('task',{}).get('phase')=='revision':
                if data['previous'].get('reply_repair'):
                    raise RuntimeError('模型请求失败（APITimeoutError）')
                result['replies']=[]
            return result
        self.provider.json=provider
        state=self.run_round()
        failed=[t for t in state['rounds'][0]['tasks'] if t.get('phase')=='revision']
        self.assertTrue(all(t['status']=='failed' and t['error_type']=='timeout' for t in failed))
        self.assertTrue(all(t['reply_repair']['pending'] for t in failed))

    def test_termination_during_repair_stops_later_requests_and_keeps_success(self):
        original=self.paired()
        self.service.store.change(self.mid, lambda s:s.update(debate_exchanges=1))
        stopped=False
        def provider(role,instruction,data):
            nonlocal stopped
            result=original(role,instruction,data)
            if data.get('task',{}).get('phase')=='peer_review':
                result['review']['challenges']*=2
            if data.get('task',{}).get('phase')=='revision':
                if data['previous'].get('reply_repair'):
                    if not stopped:
                        stopped=True
                        self.service.stop_round(self.mid,'terminate')
                    return {'replies':result['replies']}
                result['replies']=[]
            return result
        self.provider.json=provider
        self.service.start_round(self.mid,{'expected_round':0})
        state=self.state()
        self.assertEqual(state['rounds'][0]['status'],'cancelled')
        self.assertFalse(state['snapshots'])
        repaired=[t['reply_repair'] for t in state['rounds'][0]['tasks'] if t.get('reply_repair')]
        self.assertTrue(repaired)
        self.assertTrue(all(r['completed']<=1 for r in repaired))
        self.assertTrue(any(r['completed']==1 and r['pending'] for r in repaired))


if __name__ == '__main__':
    unittest.main()
