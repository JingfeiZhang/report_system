"""Targeted remediation must publish a new version and preserve frozen history."""
import copy
import unittest
from unittest.mock import patch

import test_discussion_debate as fixtures
from test_discussion import HoldingExecutor
from discussion.checkpoints import digest
from discussion.store import Conflict


class RemediationTests(unittest.TestCase):
    setUp = fixtures.DebateTests.setUp
    tearDown = fixtures.DebateTests.tearDown
    state = fixtures.DebateTests.state
    plan_tasks = fixtures.DebateTests.plan_tasks
    run_round = fixtures.DebateTests.run_round

    def source(self):
        self.plan_tasks([dict(role=r, question='核查'+r, feedback_ids=[]) for r in ('media', 'sentiment')])
        self.service.upload(self.mid, '评论.csv', 'text\n仍不能登录\n'.encode())
        original = self.provider.json
        def fail_second_exchange(role, instruction, data):
            task = data.get('task', {})
            if task.get('phase') == 'revision' and task['exchange'] == 2:
                raise RuntimeError('测试第二次修订失败')
            result = original(role, instruction, data)
            if task.get('phase') == 'peer_review' and task['exchange'] == 2:
                result['review']['challenges'] = [dict(claim_id=data['previous']['target']['claims'][0]['id'],
                    question='是否仍需限定用户范围？', reason='材料只覆盖核心服务', resolution='明确样本范围')]
            return result
        self.provider.json = fail_second_exchange
        s = self.run_round()
        self.provider.json = original
        self.provider.inputs.clear()
        self.before = {k: digest(s[k]) for k in ('rounds', 'snapshots', 'reports')}
        return s

    def assert_history(self, s):
        self.assertEqual(self.before, {k: digest(s[k][:1] if k != 'reports' else s[k]) for k in self.before})

    def test_revisions_verified_published_and_linked_without_collection(self):
        original = self.source()
        self.service.message(self.mid, dict(content='下一轮再核查传播情况', target='all'))
        with patch.object(self.service, '_search', side_effect=AssertionError('不应重新采集')):
            self.service.remediate_round(self.mid, dict(expected_round=1))
        s = self.state()
        self.assertEqual(s['status'], 'waiting', s['error'])
        self.assert_history(s)
        r = s['rounds'][-1]
        self.assertTrue(all(t['status'] == 'complete' for t in r['tasks']))
        self.assertEqual(r['verification_summary']['pending'], 0)
        self.assertEqual(r['remediation']['status'], 'complete')
        self.assertEqual(len(r['remediation']['results']), 4)
        self.assertEqual(s['snapshots'][-1]['evidence'], original['snapshots'][0]['evidence'])
        self.assertEqual(s['messages'][-1]['status'], 'pending')
        self.assertEqual({d['task']['phase'] for d in self.provider.inputs if 'task' in d}, {'revision', 'verification'})
        self.assertTrue(all(c.get('reply') and c.get('verification') for review in r['peer_reviews'] for c in review['challenges']))
        with self.assertRaises(Conflict):
            self.service.remediate_round(self.mid, dict(expected_round=2))

    def test_partial_failure_resumes_only_failed_revision(self):
        self.source()
        original = self.provider.json
        def fail_one(role, instruction, data):
            task = data.get('task', {})
            if task.get('phase') == 'revision' and task['agent_id'].endswith('media-1-1'):
                raise RuntimeError('测试临时接口不可用')
            return original(role, instruction, data)
        self.provider.json = fail_one
        self.service.remediate_round(self.mid, dict(expected_round=1))
        s = self.state()
        self.assertEqual(s['status'], 'error')
        self.assertEqual(len(s['snapshots']), 1)
        self.assertEqual(sum(t['status'] == 'complete' for t in s['rounds'][-1]['tasks']), 3)
        self.provider.json = original
        self.provider.inputs.clear()
        self.service.resume_round(self.mid)
        s = self.state()
        self.assertEqual(s['status'], 'waiting', s['error'])
        self.assertEqual(sum(d.get('task', {}).get('phase') == 'revision' for d in self.provider.inputs), 1)
        self.assert_history(s)

    def test_missing_semantic_reply_cannot_publish(self):
        self.source()
        original = self.provider.json
        def incomplete(role, instruction, data):
            result = original(role, instruction, data)
            if data.get('task', {}).get('phase') == 'verification':
                result['replies'] = []
            return result
        self.provider.json = incomplete
        self.service.remediate_round(self.mid, dict(expected_round=1))
        s = self.state()
        self.assertEqual(s['status'], 'error')
        self.assertEqual(len(s['snapshots']), 1)
        self.assertTrue(any(t['status']=='failed' and t['phase']=='verification' for t in s['rounds'][-1]['tasks']))
        self.provider.json = original
        self.provider.inputs.clear()
        self.service.resume_round(self.mid)
        s = self.state()
        self.assertEqual(s['status'], 'waiting', s['error'])
        self.assertFalse(any(d.get('task', {}).get('phase') == 'revision' for d in self.provider.inputs))
        self.assert_history(s)

    def test_duplicate_and_busy_requests_do_not_create_extra_rounds(self):
        self.source()
        self.service.pool = HoldingExecutor()
        self.service.remediate_round(self.mid, dict(expected_round=1))
        with self.assertRaises(Conflict):
            self.service.remediate_round(self.mid, dict(expected_round=1))
        self.assertEqual(len(self.state()['rounds']), 2)
        self.assertEqual(len(self.service.pool.pending), 1)


if __name__ == '__main__':
    unittest.main()
