"""Deterministic contract tests for independent contexts, exchange and snapshots."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discussion.service import DiscussionService
from discussion.store import Store
from discussion.evidence import make_evidence
from discussion.debate import evidence_context
from test_discussion import FakeProvider, InlineExecutor


class DebateProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.search_calls = []
        self.fail_second = False
        self.fail_revision = False
        self.stop_hook = None
        self.platform = None

    def json(self, role, instruction, data):
        self.inputs.append(copy.deepcopy(data))
        if '"accepted_ids"' in instruction:
            return {'accepted_ids': [e['id'] for e in data['evidence']]}
        phase = data.get('task', {}).get('phase')
        if not phase:
            return super().json(role, instruction, data)
        task, context = data['task'], data['previous']
        if self.stop_hook and phase == 'proposal':
            hook, self.stop_hook = self.stop_hook, None
            hook()
        if self.fail_second and phase == 'proposal' and task['agent_id'].endswith('-2'):
            raise RuntimeError('测试独立实例失败')
        if self.fail_revision and phase == 'revision':
            raise RuntimeError('测试修订失败')
        refs = [e['id'] for e in data['evidence'][:1]]
        result = dict(conclusion='仅确认核心服务恢复，不等于全部用户影响消除。', evidence_ids=refs,
                      limitations=['用户影响范围仍缺独立证据'], changes='限定恢复范围', response='已回应时间范围问题',
                      claims=[dict(text='核心服务10:05恢复。', kind='fact', scope='服务日志记载', evidence_ids=refs)])
        if phase == 'peer_review':
            result['review'] = dict(agreements=['认可记录中的恢复时间'], own_revision='区分恢复范围',
                challenges=[dict(claim_id=context['target']['claims'][0]['id'], question='是否所有用户恢复？',
                    reason='服务恢复不等于全体用户无影响', resolution='补充独立范围证据或限定范围', search_query='故障事件 用户恢复 范围')] if task['exchange'] == 1 else [])
            if self.platform:
                for challenge in result['review']['challenges']:
                    challenge['platform'] = self.platform
        if phase == 'revision':
            result['claims'][0].update(previous_id=context['own']['claims'][0]['id'], action='qualify')
            result['replies'] = [dict(challenge_id=c['id'], response='限定为核心服务，用户范围仍未确认', action='qualify', evidence_ids=refs)
                for r in context['peer_reviews'] for c in r['challenges'] if c['target_agent'] == task['agent_id']]
        if phase == 'verification':
            result['verifications'] = [dict(claim_id=c['id'], status='supported', reason='原文明确限定为核心服务', evidence_ids=refs)
                for p in context['candidates'] for c in p['claims']]
            result['replies'] = [dict(challenge_id=c['id'], resolved=True, reason='明确限定范围', evidence_ids=refs)
                for r in context['peer_reviews'] for c in r['challenges']]
        return result

    def search(self, query, time_range):
        self.search_calls.append(query)
        return [make_evidence('新增核查：仅核心服务恢复，无法确定用户影响范围。', '补充范围说明', '段落1', 'web_summary', source_id='supplement')]


class DebateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = DebateProvider()
        self.service = DiscussionService(Store(self.tmp.name), self.provider)
        self.service.pool.shutdown()
        self.service.pool = InlineExecutor()
        state = self.service.create(dict(title='故障事件', mode='live', web_enabled=False))
        self.mid = state['id']
        self.service.upload(self.mid, '资料.txt', '故障时间线服务日志：09:10异常，10:05核心服务恢复，10:40核对数据。'.encode())

    def tearDown(self):
        self.tmp.cleanup()

    def run_round(self):
        self.service.start_round(self.mid, {'expected_round': len(self.state()['rounds'])})
        self.assertEqual(self.state()['status'], 'waiting', self.state()['error'])
        return self.state()

    def state(self):
        return self.service.store.get(self.mid)

    def plan_tasks(self, tasks):
        original = self.provider.json
        def call(role, instruction, data):
            if '"tasks"' in instruction:
                self.provider.inputs.append(copy.deepcopy(data))
                return {'tasks': copy.deepcopy(tasks)}
            return original(role, instruction, data)
        self.provider.json = call
        self.service.store.change(self.mid, lambda s: s.update(roles=[t['role'] for t in tasks]))

    def test_media_and_sentiment_get_real_reciprocal_reviews(self):
        # Missing or stale debate:false flags cannot silently turn interpretation into a digest.
        self.plan_tasks([dict(role=r, question='分析'+r, debate=False, feedback_ids=[])
                         for r in ('media', 'sentiment', 'facts')])
        self.service.upload(self.mid, '评论.csv', 'text,source\n仍不能登录,测试平台\n'.encode())
        state = self.run_round()
        round_ = state['rounds'][0]
        for issue in round_['issues']:
            self.assertEqual(issue['mode'], 'debate', issue)
            self.assertEqual(len(issue['agents']), 2)
            reviews = [r for r in round_['peer_reviews'] if r['issue_id'] == issue['id']]
            self.assertEqual({r['author'] for r in reviews}, set(issue['agents']))
            self.assertTrue(all(c.get('reply') and c['status'] == 'resolved' for r in reviews for c in r['challenges']))
            inputs = [d for d in self.provider.inputs if d.get('task', {}).get('issue_id') == issue['id'] and d['task']['phase'] == 'proposal']
            self.assertEqual(len(inputs), 2)
            self.assertEqual(inputs[0]['previous'], inputs[1]['previous'])
            self.assertEqual(inputs[0]['evidence'], inputs[1]['evidence'])
            self.assertEqual(inputs[0]['sentiment'], inputs[1]['sentiment'])
            self.assertNotEqual(inputs[0]['task']['instruction'], inputs[1]['task']['instruction'])
        self.assertTrue(round_['report_ready'])

    def test_media_and_sentiment_cannot_downgrade_to_digest_or_aggregation(self):
        self.plan_tasks([
            dict(role='facts', question='整理来源目录', analysis_kind='source_digest', debate=False, reason='只整理出处'),
            dict(role='media', question='整理媒体来源', analysis_kind='source_digest', debate=False),
            dict(role='sentiment', question='列出统计结果', analysis_kind='aggregation', debate=False)])
        self.service.upload(self.mid, '评论.csv', 'text\n仍不能登录\n'.encode())
        state = self.run_round()
        issues = {i['role']: i for i in state['rounds'][0]['issues']}
        self.assertEqual({r:i['mode'] for r,i in issues.items()}, dict(facts='single', media='debate', sentiment='debate'))
        for role in ('media', 'sentiment'):
            reviews = [r for r in state['rounds'][0]['peer_reviews'] if r['issue_id']==issues[role]['id']]
            self.assertEqual({r['author'] for r in reviews}, set(issues[role]['agents']))
            self.assertTrue(all(r['challenges'] for r in reviews if r['exchange']==1))

    def test_posts_allow_qualitative_sentiment_debate_without_comment_ratios(self):
        self.plan_tasks([dict(role='sentiment', question='比较帖子中的用户诉求')])
        post = make_evidence('用户反映登录失败，希望尽快恢复。', '用户帖子', '正文', 'social_post')
        self.service.store.change(self.mid, lambda s: s['evidence'].append(post))
        state = self.run_round()
        self.assertEqual(state['rounds'][0]['issues'][0]['mode'], 'debate')
        self.assertIsNone(state['snapshots'][0]['sentiment'])
        proposals = [d for d in self.provider.inputs if d.get('task', {}).get('phase') == 'proposal']
        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(d['sample_scope']['comment_count'] == 0 for d in proposals))
        self.assertTrue(all('只有帖子时仅作定性分析' in d['task']['instruction'] for d in proposals))

    def test_no_opinion_material_uses_real_calls_with_explicit_scope(self):
        self.plan_tasks([dict(role='sentiment', question='分析用户诉求', debate=True)])
        state = self.run_round()
        issue = state['rounds'][0]['issues'][0]
        self.assertEqual(issue['mode'], 'debate')
        proposals=[d for d in self.provider.inputs if d.get('task',{}).get('phase')=='proposal']
        self.assertEqual(len(proposals),2)
        self.assertTrue(all(d['sample_scope']['comment_count']==0 for d in proposals))
        self.assertTrue(all('没有用户表达材料时' in d['task']['instruction'] for d in proposals))
        self.assertIsNone(state['snapshots'][0]['sentiment'])

    def test_all_three_issues_finish_two_exchanges_and_maximum_verification(self):
        self.plan_tasks([dict(role=r, question='核查'+r, debate=True) for r in ('facts', 'media', 'sentiment')])
        self.service.upload(self.mid, '评论.csv', 'text\n仍不能登录\n'.encode())
        original = self.provider.json
        def maximum(role, instruction, data):
            result = original(role, instruction, data)
            phase = data.get('task', {}).get('phase')
            context = data.get('previous')
            if phase in ('proposal', 'revision'):
                result['claims'] = [dict(result['claims'][0], text=f'服务日志记录节点 {j}') for j in range(3)]
                if phase == 'revision':
                    for j, claim in enumerate(result['claims']):
                        claim['previous_id'] = context['own']['claims'][j]['id']
            if phase == 'peer_review':
                result['review']['challenges'] = [dict(claim_id=c['id'], question='请核查该节点', reason='需要限定节点含义', resolution='限定范围', search_query='') for c in context['target']['claims'][:2]]
            return result
        self.provider.json = maximum
        state = self.run_round()
        round_ = state['rounds'][0]
        self.assertEqual(round_['budget']['analysis_calls'], 51)
        self.assertEqual(round_['budget']['analysis_limit'], 51)
        self.assertEqual(len(round_['peer_reviews']), 12)
        self.assertTrue(all(i['exchange'] == 2 for i in round_['issues']))
        self.assertTrue(all(c['evidence_status'] == 'supported' for c in state['snapshots'][0]['claims']))
        self.assertTrue(all(c['status'] == 'resolved' for r in round_['peer_reviews'] for c in r['challenges']))
        self.assertTrue(round_['report_ready'])

    def test_agreement_still_checks_a_specific_claim_without_forced_opposition(self):
        self.plan_tasks([dict(role='media', question='比较观点')])
        original = self.provider.json
        def agree(role, instruction, data):
            result = original(role, instruction, data)
            if data.get('task', {}).get('phase') == 'peer_review':
                result['review']['agreements'] = ['认可对方结论，仍需核对范围']
                if data['task']['exchange']==1:
                    result['review']['challenges'][0]['question'] = '认可该结论，请说明原文如何界定适用范围？'
                else:
                    result['review']['challenges'] = []
            return result
        self.provider.json = agree
        state = self.run_round()
        reviews=state['rounds'][0]['peer_reviews']
        self.assertEqual(len(reviews),4)
        self.assertTrue(all(r['challenges'] for r in reviews if r['exchange']==1))
        self.assertTrue(all(r['challenges']==[] for r in reviews if r['exchange']==2))

    def test_missing_reciprocal_question_is_retried_and_not_cached_as_success(self):
        self.plan_tasks([dict(role='media', question='比较媒体观点')])
        original=self.provider.json
        missing=[True]
        def incomplete(role,instruction,data):
            result=original(role,instruction,data)
            if missing[0] and data.get('task',{}).get('phase')=='peer_review':
                result['review']['challenges']=[]
            return result
        self.provider.json=incomplete
        state=self.run_round()
        issue=state['rounds'][0]['issues'][0]
        self.assertEqual(issue['status'],'unresolved')
        self.assertFalse(state['rounds'][0]['peer_reviews'])
        failed=[t for t in state['rounds'][0]['tasks'] if t.get('phase')=='peer_review']
        self.assertTrue(all(t['status']=='failed' and t['attempts']==2 for t in failed))
        self.assertTrue(any('不能视为已达成共识' in w for w in state['rounds'][0]['warnings']))

    def test_revision_must_answer_all_incoming_questions(self):
        self.plan_tasks([dict(role='sentiment', question='核查情绪解释')])
        self.service.upload(self.mid,'评论.csv','text\n还是无法使用\n'.encode())
        original=self.provider.json
        def omit(role,instruction,data):
            result=original(role,instruction,data)
            if data.get('task',{}).get('phase')=='revision':
                result['replies']=[]
            return result
        self.provider.json=omit
        state=self.run_round()
        self.assertTrue(all(c['version']==1 for c in state['snapshots'][0]['cards']))
        self.assertTrue(all(c['status']=='open' for r in state['rounds'][0]['peer_reviews'] for c in r['challenges']))

    def test_legacy_plan_keeps_its_original_instance_routing(self):
        self.plan_tasks([dict(role=r, question='核查'+r, debate=False) for r in ('facts', 'media')])
        original = self.service._tasks
        def legacy(state, feedback):
            tasks = original(state, feedback)
            for t in tasks:
                t.pop('collaboration_version')
                t['debate'] = False
            return tasks
        self.service._tasks = legacy
        state = self.run_round()
        self.assertEqual([i['mode'] for i in state['rounds'][0]['issues']], ['debate', 'single'])

    def test_isolation_barriers_reciprocal_reviews_and_versions(self):
        state = self.run_round()
        proposals = [d for d in self.provider.inputs if d.get('task', {}).get('phase') == 'proposal']
        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(d['previous'] == [] for d in proposals))
        self.assertEqual(proposals[0]['task']['question'], proposals[1]['task']['question'])
        self.assertEqual(proposals[0]['evidence'], proposals[1]['evidence'])
        for exchange in (1, 2):
            inputs = [d for d in self.provider.inputs if d.get('task', {}).get('phase') == 'revision' and d['task']['exchange'] == exchange]
            self.assertEqual(len(inputs), 2)
            self.assertEqual(inputs[0]['previous']['frozen_candidates'], inputs[1]['previous']['frozen_candidates'])
            self.assertTrue(all(c['version'] == exchange for c in inputs[0]['previous']['frozen_candidates']))
        snap = state['snapshots'][0]
        self.assertEqual(len(snap['cards']), 2)
        self.assertEqual(len({p['agent_id'] for p in snap['cards']}), 2)
        self.assertTrue(all(c['version'] == 3 for c in snap['cards']))
        self.assertTrue(all(c['previous_id'] for p in snap['cards'] for c in p['claims']))
        self.assertTrue(all(c['evidence_status'] == 'supported' for c in snap['claims']))
        self.assertTrue(all(c['status'] == 'resolved' for r in snap['peer_reviews'] for c in r['challenges']))
        events = [e['type'] for e in self.service.store.events(self.mid)]
        self.assertLess(events.index('proposal_set_frozen'), events.index('peer_review_posted'))

    def test_failure_is_degraded_not_debate(self):
        self.provider.fail_second = True
        state = self.run_round()
        self.assertEqual(state['rounds'][0]['issues'][0]['mode'], 'degraded')
        self.assertEqual(state['rounds'][0]['peer_reviews'], [])
        self.assertTrue(any('单答案降级' in w for w in state['snapshots'][0]['warnings']))

    def test_paired_role_failure_cannot_be_marked_as_completed_dialogue(self):
        self.plan_tasks([dict(role='media',question='核查媒体观点')])
        self.provider.fail_second=True
        state=self.run_round()
        issue=state['rounds'][0]['issues'][0]
        self.assertEqual(issue['mode'],'degraded')
        self.assertEqual(issue['status'],'unresolved')
        self.assertFalse(state['rounds'][0]['peer_reviews'])

    def test_failed_revision_retains_proposals_and_unresolved_questions(self):
        self.provider.fail_revision = True
        state = self.run_round()
        self.assertTrue(all(c['version'] == 1 for c in state['snapshots'][0]['cards']))
        self.assertTrue(any(c['status'] == 'open' for r in state['rounds'][0]['peer_reviews'] for c in r['challenges']))
        self.assertEqual(state['rounds'][0]['issues'][0]['status'], 'unresolved')

    def test_finish_exchange_still_verifies_claims(self):
        self.provider.stop_hook = lambda: self.service.stop_round(self.mid)
        state = self.run_round()
        self.assertEqual(state['rounds'][0]['peer_reviews'], [])
        self.assertTrue(all(c['evidence_status'] == 'supported' for c in state['snapshots'][0]['claims']))

    def test_terminate_does_not_freeze_snapshot_and_resume_reuses_calls(self):
        self.provider.stop_hook = lambda: self.service.stop_round(self.mid, 'terminate')
        self.service.start_round(self.mid, {'expected_round': 0})
        state = self.state()
        self.assertEqual(state['rounds'][0]['status'], 'cancelled')
        self.assertEqual(state['snapshots'], [])
        completed_agents = {c['agent_id'] for c in state['rounds'][0]['cards'] if c.get('phase') == 'proposal'}
        proposal_calls = {agent: sum(d.get('task', {}).get('phase') == 'proposal' and d['task']['agent_id'] == agent
                                    for d in self.provider.inputs) for agent in completed_agents}
        self.service.resume_round(self.mid)
        self.assertEqual(self.state()['status'], 'waiting', self.state()['error'])
        self.assertEqual(len(self.state()['snapshots']), 1)
        for agent, count in proposal_calls.items():
            self.assertEqual(sum(d.get('task', {}).get('phase') == 'proposal' and d['task']['agent_id'] == agent
                                 for d in self.provider.inputs), count)
        self.assertEqual(len({c['id'] for c in self.state()['rounds'][0]['cards']}), len(self.state()['rounds'][0]['cards']))

    def test_terminate_during_collection_does_not_start_sentiment_or_proposals(self):
        self.plan_tasks([dict(role='sentiment', question='检查评论')])
        self.service.upload(self.mid, '评论.csv', 'text,source\n仍不能登录,测试平台\n'.encode())
        with patch.object(self.service, '_collect_social', side_effect=lambda *args: self.service.stop_round(self.mid, 'terminate')):
            with patch.object(self.service, '_sentiment') as sentiment:
                self.service.start_round(self.mid, {'expected_round': 0})
                sentiment.assert_not_called()
        state = self.state()
        self.assertEqual(state['rounds'][0]['status'], 'cancelled')
        self.assertIsNone(state['operation'])
        self.assertEqual(state['snapshots'], [])
        self.assertFalse(any(d.get('task', {}).get('phase') for d in self.provider.inputs))

    def test_terminated_sentiment_batches_do_not_call_provider(self):
        from discussion.sampling import classify
        comments = [dict(id=f'c{i}', text=f'评论 {i}') for i in range(100)]
        with patch.object(self.provider, 'json') as call:
            result = classify(self.service.store, self.provider, comments, stopped=lambda: True)
            call.assert_not_called()
        self.assertEqual(result['classified'], 0)
        self.assertEqual(result['failed'], 100)

    def test_summary_failure_resume_reuses_all_analysis(self):
        original = self.provider.json
        fail = [True]
        def call(role, instruction, data):
            if '"overview"' in instruction and fail[0]:
                raise RuntimeError('模拟主持失败')
            return original(role, instruction, data)
        self.provider.json = call
        self.service.start_round(self.mid, {'expected_round': 0})
        self.assertEqual(self.state()['status'], 'error')
        count = len(self.provider.inputs)
        fail[0] = False
        self.service.resume_round(self.mid)
        self.assertEqual(self.state()['status'], 'waiting', self.state()['error'])
        new = self.provider.inputs[count:]
        self.assertFalse(any(d.get('task', {}).get('phase') for d in new))
        self.assertEqual(len(self.state()['rounds']), 1)

    def test_collection_stop_does_not_stop_verification(self):
        self.provider.stop_hook = lambda: self.service.stop_collection(self.mid)
        state = self.run_round()
        self.assertTrue(state['rounds'][0]['collection_stop_requested'])
        self.assertTrue(state['rounds'][0]['verification_summary']['checked'])
        self.assertFalse(state['rounds'][0]['stop_requested'])

    def test_missing_verifications_retry_without_repeating_successful_proposals(self):
        self.service.store.change(self.mid, lambda s: s.update(report_depth='full'))
        original = self.provider.json
        missing = [True]
        def call(role, instruction, data):
            result = original(role, instruction, data)
            if missing[0] and data.get('task', {}).get('phase') == 'verification':
                result['verifications'] = []
            return result
        self.provider.json = call
        self.service.start_round(self.mid, {'expected_round': 0})
        self.assertEqual(self.state()['status'], 'error')
        count = len(self.provider.inputs)
        missing[0] = False
        self.service.resume_round(self.mid)
        self.assertEqual(self.state()['status'], 'waiting', self.state()['error'])
        self.assertTrue(self.state()['rounds'][0]['report_ready'])
        phases = [d.get('task', {}).get('phase') for d in self.provider.inputs[count:]]
        self.assertIn('verification', phases)
        self.assertNotIn('proposal', phases)
        self.assertNotIn('revision', phases)

    def test_supplement_is_shared_and_real_not_fabricated(self):
        self.service.store.change(self.mid, lambda s: s.update(web_enabled=True))
        state = self.run_round()
        self.assertEqual(self.provider.search_calls.count('故障事件 用户恢复 范围'), 1)
        self.assertTrue(any(t['status'] in ('complete', 'duplicates') for t in state['rounds'][0]['tool_runs']))
        revision_inputs = [d for d in self.provider.inputs if d.get('task', {}).get('phase') == 'revision']
        self.assertTrue(all(any(e['source_id'] == 'supplement' for e in d['evidence']) for d in revision_inputs))
        self.assertTrue(all(d['previous']['supplement_results'] for d in revision_inputs))
        self.assertEqual(revision_inputs[0]['previous']['supplement_results'], revision_inputs[1]['previous']['supplement_results'])
        self.assertTrue(any(t['status']=='reused' for t in state['rounds'][0]['tool_runs']))

    def test_previous_snapshot_immutable_and_report_uses_latest(self):
        first = self.run_round()['snapshots'][0]
        self.service.message(self.mid, {'content': '说明恢复的范围', 'target': 'facts'})
        state = self.run_round()
        self.assertEqual(first, state['snapshots'][0])
        self.assertEqual(state['messages'][0]['resolution'], 'responded')
        self.assertEqual(len(state['snapshots'][1]['cards']), 2)

    def test_stopped_collection_still_revises_and_verifies(self):
        self.service.store.change(self.mid, lambda s: s.update(web_enabled=True))
        original = self.provider.json
        def provider(role, instruction, data):
            if data.get('task', {}).get('phase') == 'peer_review':
                for collector in self.service.collectors.values():
                    self.service.stop_collection(self.mid)
            return original(role, instruction, data)
        self.provider.json = provider
        state = self.run_round()
        tools = state['rounds'][0]['tool_runs']
        skipped = [t for t in tools if t['status']=='user_stopped']
        self.assertTrue(skipped)
        self.assertTrue(all(t['request_count']==0 and t['local_evidence_ids'] for t in skipped))
        self.assertNotIn('故障事件 用户恢复 范围', self.provider.search_calls)
        self.assertTrue(all(c['evidence_status']=='supported' for c in state['snapshots'][0]['claims']))
        revisions = [d for d in self.provider.inputs if d.get('task', {}).get('phase')=='revision']
        self.assertTrue(all(any(t['status']=='user_stopped' for t in d['previous']['supplement_results']) for d in revisions))

    def test_original_references_not_lost_in_top_k(self):
        pool = [make_evidence(f'材料{i}', f'来源{i}', '第一段') for i in range(20)]
        required = pool[-1]['id']
        selected = evidence_context(pool, {'conclusion': '['+required+']'}, None, '材料0')
        self.assertIn(required, [e['id'] for e in selected])
        with self.assertRaises(RuntimeError):
            evidence_context(pool, {'conclusion': '[E-000000000000]'}, None, '材料')

    def test_verification_is_compact_and_timeout_retries_only_failed_batch(self):
        original = self.provider.json
        attempts = {}
        def provider(role, instruction, data):
            if data.get('task', {}).get('phase') == 'verification':
                tid = data['task']['id']
                attempts[tid] = attempts.get(tid, 0)+1
                context = data['previous']
                self.assertLessEqual(len(context['required_claim_ids']), 2)
                self.assertTrue(all(set(p)=={'agent_id','claims'} for p in context['candidates']))
                if context['required_claim_ids'] and attempts[tid]==1:
                    raise RuntimeError('模型请求失败（APITimeoutError），可重试')
            return original(role, instruction, data)
        self.provider.json = provider
        state = self.run_round()
        self.assertTrue(all(c['evidence_status']=='supported' for c in state['snapshots'][0]['claims']))
        self.assertIn(2, attempts.values())
        self.assertIn(1, attempts.values())
        self.assertTrue(all(t.get('elapsed_seconds', 0)>=0 for t in state['rounds'][0]['tasks']))

    def test_permanent_verification_timeout_preserves_other_batches(self):
        original = self.provider.json
        def provider(role, instruction, data):
            if data.get('task', {}).get('phase')=='verification' and data['previous']['required_claim_ids']:
                raise RuntimeError('模型请求失败（APITimeoutError），可重试')
            return original(role, instruction, data)
        self.provider.json = provider
        state = self.run_round()
        failed = [t for t in state['rounds'][0]['tasks'] if t['status']=='failed']
        self.assertTrue(failed)
        self.assertTrue(all(t['attempts']==2 for t in failed))
        self.assertTrue(any(t['status']=='complete' and t.get('phase')=='verification' for t in state['rounds'][0]['tasks']))
        self.assertEqual(state['rounds'][0]['issues'][0]['status'], 'unresolved')

    def test_platform_supplement_updates_evidence_and_frozen_statistics(self):
        self.provider.platform = 'wb'
        self.service.store.change(self.mid, lambda s: s.update(crawl_platforms=['wb']))
        calls = []
        class Crawler:
            def collect_page(inner, mid, n, task, remaining, stopped):
                result, evidence, comments = inner.collect(mid, n, task['platform'], task['query'])
                result.update(has_more=False, posts=[], comments=[], page=1, cursor={'page':1}, request_count=1)
                return result, evidence, comments
            def collect(inner, mid, n, platform, query):
                calls.append(query)
                e = make_evidence('故障时间线 用户恢复评论：还有异常', '平台评论', '评论', 'comment', source_id=query)
                c = {'id': e['id']+'-comment', 'evidence_id': e['id'], 'text': '还有异常 '+query, 'source': '微博'}
                return dict(platform=platform, platform_name='微博', query=query, status='complete', message='测试采集完成', post_count=1, comment_count=1), [e], [c]
        self.service.crawler = Crawler()
        state = self.run_round()
        self.assertEqual(calls.count('故障事件 用户恢复 范围'), 1)
        self.assertEqual(state['snapshots'][0]['sentiment']['total'], 2)
        record = next(t for t in state['rounds'][0]['tool_runs'] if t.get('platform') == 'wb')
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(len(record['new_evidence_ids']), 1)

    def test_targeted_claim_feedback_carries_version_and_rejects_other_meeting(self):
        state = self.run_round()
        claim = state['snapshots'][0]['cards'][0]['claims'][0]
        self.service.message(self.mid, {'content': '核查该主张', 'claim_id': claim['id']})
        message = self.state()['messages'][-1]
        self.assertEqual(message['target'], 'facts')
        self.assertEqual(message['claim_context']['version'], claim['version'])
        with self.assertRaises(ValueError):
            self.service.message(self.mid, {'content': '核查', 'claim_id': 'another-meeting-claim'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
