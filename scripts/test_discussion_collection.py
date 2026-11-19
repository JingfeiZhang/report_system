"""Paged acquisition, budget, persistence, sampling and migration contracts."""
import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
import sys
import types
from threading import Barrier, Event, Lock
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch

from test_discussion import FakeProvider, HoldingExecutor, DiscussionTests
from test_discussion_crawler import SocialProvider
from discussion.collection import RoundCollector, public_task, explain_supplements, supplement_outcome
from discussion.crawler import normalize
from discussion.crawler_worker import collect
from discussion.evidence import make_evidence, retrieve, retrieval_index
from discussion.sampling import classify
from discussion.store import Store


class Pages:
    def __init__(self, pages=3, stop=None, repeated=False, blocked=None):
        self.pages, self.stop, self.repeated, self.blocked = pages, stop, repeated, blocked
        self.calls = []

    def collect_page(self, mid, number, task, remaining, stopped):
        page = task.get('cursor', {}).get('page', 0)+1
        self.calls.append((task['platform'], task['kind'], page))
        if task['platform'] == self.blocked:
            return dict(status='needs_login', posts=[], comments=[], request_count=1), [], []
        posts = [dict(id=f'p{page}_{i}', text=f'故障事件材料{page}_{i}', date='2026-11-17') for i in range(8)]
        if self.repeated:
            posts = [dict(id='duplicate', text='故障事件重复材料')]
        raw = dict(posts=posts, comments=[], status='complete', has_more=page<self.pages, request_count=1,
                   page=page, cursor={'page':page,'next':page+1}, raw_page=f'test-page-{page}')
        evidence, comments = normalize(raw, task['platform'], task['query'], '2026-11-17')
        if self.stop:
            self.stop()
        return raw, evidence, comments


class CollectionTests(DiscussionTests):
    def setUp(self):
        super().setUp()
        self.service.provider = SocialProvider()
        self.mid = self.create('live')
        self.service.pool = HoldingExecutor()
        self.service.update_sources(self.mid, dict(crawl_platforms=['wb', 'xhs']))
        self.service.start_round(self.mid, dict(expected_round=0))
        self.s = self.state(self.mid)
        self.collector = RoundCollector(self.service, self.s, self.s['operation'], [], [])

    def run_pages(self, crawler):
        self.service.crawler = crawler
        return self.collector.run(plans=[dict(platform=p, query='故障事件', question='故障时间线', condition='平台回应', comments=False) for p in ['wb','xhs']])

    def test_pages_exceed_five_and_fair_sources(self):
        crawler = Pages()
        records = self.run_pages(crawler)
        for platform in ('wb', 'xhs'):
            self.assertEqual([page for p, kind, page in crawler.calls if p==platform], [1, 2, 3])
        self.assertEqual(len(self.state(self.mid)['evidence']), 48)
        self.assertTrue(all(r['post_count']==24 and r['stop_reason']=='exhausted' for r in records))
        self.assertNotIn('cursor', public_task(self.collector.records[0]))

    def test_repeated_pages_stall_not_infinite(self):
        records = self.run_pages(Pages(pages=100, repeated=True))
        self.assertTrue(all(r['page']==4 and r['stop_reason']=='stalled' for r in records))
        self.assertEqual(len(self.state(self.mid)['evidence']), 2)

    def test_blocked_platform_does_not_block_other_source(self):
        records = self.run_pages(Pages(blocked='wb'))
        self.assertEqual(records[0]['stop_reason'], 'needs_login')
        self.assertEqual(records[1]['post_count'], 24)
        self.assertEqual(records[1]['stop_reason'], 'exhausted')

    def test_stop_and_resume_ingests_saved_page_before_next_request(self):
        self.run_pages(Pages(stop=lambda: self.service.stop_collection(self.mid)))
        first_calls = list(self.service.crawler.calls)
        self.assertTrue(1 <= len(first_calls) <= 2)  # Other platform may already be in flight.
        self.assertTrue(all(page==1 for _, _, page in first_calls))
        for platform, _, _ in first_calls:
            self.assertTrue(self.store_task(platform).get('pending_ingest'))
        self.service.store.change(self.mid, lambda s: s['rounds'][-1].update(collection_stop_requested=False))
        self.collector = RoundCollector(self.service, self.state(self.mid), self.s['operation'], [], [])
        self.run_pages(Pages())
        for call in first_calls:
            self.assertNotIn(call, self.service.crawler.calls)
        self.assertEqual(len(self.state(self.mid)['evidence']), 48)

    def store_task(self, platform):
        return next(t for t in self.service.store.collection_tasks(self.mid) if t['platform']==platform)

    def test_old_300_seconds_setting_does_not_stop_pages_or_supplement(self):
        self.s['collection_budget_seconds'] = 300
        clock = [0.0]
        crawler = Pages(stop=lambda: clock.__setitem__(0, clock[0]+301))
        with patch('discussion.collection.time.monotonic', side_effect=lambda: clock[0]):
            records = self.run_pages(crawler)
            self.service._search_unbudgeted = lambda *args: [make_evidence('补证新材料', '来源', '摘要')]
            self.assertTrue(self.collector.search('补证'))
        self.assertEqual(len(crawler.calls), 6)
        self.assertTrue(all(r['page']==3 for r in records))
        progress = self.state(self.mid)['rounds'][-1]['collection_budget']
        self.assertEqual(progress['mode'], 'adaptive')
        self.assertGreater(progress['used_seconds'], 300)
        self.assertNotIn('remaining_seconds', progress)

    def test_platforms_and_web_overlap_with_serial_pages_per_platform(self):
        barrier, lock = Barrier(4, timeout=8), Lock()
        active, peak, entered, overlaps = {}, {}, set(), []
        class ConcurrentPages(Pages):
            def collect_page(inner, mid, number, task, remaining, stopped):
                p = task['platform']
                with lock:
                    active[p] = active.get(p, 0)+1
                    peak[p] = max(peak.get(p, 0), active[p])
                    first = p not in entered
                    entered.add(p)
                    overlaps.append(sum(active.values()))
                try:
                    if first:
                        barrier.wait()
                    return super().collect_page(mid, number, task, remaining, stopped)
                finally:
                    with lock:
                        active[p] -= 1
        self.service.crawler = ConcurrentPages(pages=2)
        web = make_evidence('网页故障原始回应', '网页资料', '摘要')
        def search(*args):
            barrier.wait()
            return [web]
        self.service._search_unbudgeted = search
        plans = [dict(platform=p, query=q, question='故障', comments=False)
                 for p in ('wb','xhs','dy') for q in ('故障', '故障回应')]
        records = self.collector.run(plans=plans, web_queries=['故障'])
        self.assertEqual(peak, dict(wb=1, xhs=1, dy=1))
        self.assertEqual(max(overlaps), 3)
        self.assertEqual(len(self.service.crawler.calls), 12)
        self.assertEqual(len(self.state(self.mid)['evidence']), 49)
        self.assertEqual(self.collector.request_count, 13)
        self.assertTrue(all(r['status'] in ('model_stopped','exhausted') for r in records))

    def test_slow_relevance_does_not_hold_other_platform(self):
        slow_entered, other_finished, released = Event(), Event(), Event()
        class Provider(SocialProvider):
            def json(inner, role, instruction, data):
                if '"accepted_ids"' in instruction and data.get('evidence'):
                    platform = data['evidence'][0].get('platform')
                    if platform == 'wb' and not slow_entered.is_set():
                        slow_entered.set()
                        if other_finished.wait(8):
                            released.set()
                return super().json(role, instruction, data)
        class Crawler(Pages):
            def collect_page(inner, mid, number, task, remaining, stopped):
                if task['platform']=='xhs':
                    if not slow_entered.wait(8):
                        raise RuntimeError('微博筛选未启动')
                    if task.get('cursor', {}).get('page')==1:
                        other_finished.set()
                return super().collect_page(mid, number, task, remaining, stopped)
        self.service.provider = Provider()
        self.run_pages(Crawler(pages=2))
        self.assertTrue(released.is_set(), '小红书第二页应在微博筛选完成之前执行')
        self.assertEqual(len(self.state(self.mid)['evidence']), 32)

    def test_web_only_and_shared_wall_clock(self):
        clock = [10.0]
        def search(*args):
            clock[0] += 12
            return [make_evidence('网页资料', '来源', '摘要')]
        self.service._search_unbudgeted = search
        with patch('discussion.collection.time.monotonic', side_effect=lambda: clock[0]):
            records = self.collector.run(plans=[], web_queries=['事件'])
            self.assertEqual(self.collector.used, 12)
            self.collector.search('追问')
            self.assertEqual(self.collector.used, 24)
            self.assertNotIn('remaining_seconds', self.state(self.mid)['rounds'][-1]['collection_budget'])
        self.assertEqual(records[0]['status'], 'model_stopped')
        self.assertEqual(len(self.state(self.mid)['evidence']), 1)

    def test_model_refines_web_query_then_stops_with_real_references(self):
        class Provider(SocialProvider):
            def json(inner, role, instruction, data):
                if '"covered"' in instruction:
                    inner.asserted_condition = data['condition']
                    if len(data['attempted_queries']) == 1:
                        return dict(covered=False, action='refine', next_query='原始回应', reason='缺原文', gaps=['原文'], evidence_ids=[])
                    return dict(covered=True, action='stop', reason='原文已找到', evidence_ids=[data['evidence'][0]['id']])
                return super().json(role,instruction,data)
        self.service.provider = Provider()
        queries = []
        def search(query,*args):
            queries.append(query)
            return [make_evidence('原始回应 '+query, query, '段落')]
        self.service._search_unbudgeted = search
        self.assertEqual(len(self.collector.search('事件', question='是否取得原始回应', condition='必须取得原始回应页面')),2)
        self.assertEqual(queries,['事件','原始回应'])
        self.assertEqual(self.collector.records[-1]['status'],'satisfied')
        self.assertEqual(len(self.state(self.mid)['evidence']),2)
        self.assertEqual(self.service.provider.asserted_condition, '必须取得原始回应页面')
        outcome = supplement_outcome(self.collector.records[0], [e['id'] for e in self.collector.evidence], self.collector.records)
        self.assertEqual(outcome['request_count'], 2)
        self.assertEqual(len(outcome['collection_task_ids']), 2)

    def test_social_refinement_creates_new_query_and_keeps_completed_pages(self):
        class Provider(SocialProvider):
            def json(inner,role,instruction,data):
                if '"covered"' in instruction:
                    if data['statistics']['query']=='故障事件':
                        return dict(covered=False, action='refine', reason='需按回应关键词定向查找', next_query='故障事件 原始回应', evidence_ids=[])
                    return dict(covered=False, action='stop', reason='本平台无原始回应，转入分析保留缺口', evidence_ids=[], gaps=['原始回应'])
                return super().json(role,instruction,data)
        self.service.provider=Provider()
        records=self.run_pages(Pages(pages=20))
        self.assertEqual(len(self.service.crawler.calls),4)
        self.assertEqual(len(records),4)
        self.assertTrue(all(r['page']==1 for r in records))
        self.assertTrue(all(r['assessment']['status']=='gaps' for r in records))

    def test_assessment_failure_pauses_without_claiming_sufficiency(self):
        class Provider(SocialProvider):
            def json(inner, role, instruction, data):
                if '"covered"' in instruction:
                    raise TimeoutError('assessment timeout')
                return super().json(role,instruction,data)
        self.service.provider=Provider()
        records=self.run_pages(Pages(pages=99))
        self.assertTrue(all(r['status']=='assessment_failed' for r in records))
        self.assertTrue(all(r['assessment']['status']=='unevaluated' for r in records))
        self.assertEqual(len(self.service.crawler.calls),2)

    def test_model_stops_before_pagination_exhausts(self):
        class Provider(SocialProvider):
            def json(inner,role,instruction,data):
                if '"covered"' in instruction:
                    return dict(covered=True, action='stop', reason='取得原文', evidence_ids=[data['evidence'][0]['id']])
                return super().json(role,instruction,data)
        self.service.provider=Provider()
        records=self.run_pages(Pages(pages=99))
        self.assertEqual(len(self.service.crawler.calls),2)
        self.assertTrue(all(r['status']=='satisfied' for r in records))

    def test_model_can_try_new_direction_after_three_empty_batches(self):
        class Provider(SocialProvider):
            def json(inner,role,instruction,data):
                if '"covered"' in instruction:
                    if len(data['attempted_queries']) >= 4:
                        return dict(covered=False,action='stop',reason='已检查四个不同方向，保留缺口',evidence_ids=[])
                    return dict(covered=False,action='refine',reason='缺原文',evidence_ids=[],next_query='查询'+str(len(data['attempted_queries'])))
                return super().json(role,instruction,data)
        self.service.provider=Provider()
        self.service._search_unbudgeted=lambda *args: []
        self.collector.search('事件')
        self.assertEqual(self.collector.request_count,4)
        self.assertEqual(self.collector.records[-1]['status'],'model_stopped')

    def test_search_last_page_keeps_comment_queue_and_reassesses_after_comments(self):
        assessments = []
        class Provider(SocialProvider):
            def json(inner, role, instruction, data):
                if '"covered"' in instruction:
                    assessments.append(copy.deepcopy(data))
                return super().json(role, instruction, data)
        class Crawler:
            calls = []
            def collect_page(inner, mid, n, task, remaining, stopped):
                inner.calls.append(task['kind'])
                post = dict(id='p1', text='故障事件原始记录')
                raw = dict(status='complete', posts=[post] if task['kind']=='search' else [],
                           parents=[post], comments=[] if task['kind']=='search' else [dict(id='c1',parent_id='p1',text='故障事件评论')],
                           has_more=False, page=1, cursor={'page':1,'next':0}, request_count=1)
                evidence, comments = normalize(raw, 'wb', '故障事件', 'today')
                return raw, evidence, comments
        self.service.provider = Provider()
        self.service.crawler = Crawler()
        self.collector.run(plans=[dict(platform='wb', query='故障事件', comments=True)])
        self.assertEqual(self.service.crawler.calls, ['search', 'comments'])
        self.assertEqual(len(self.state(self.mid)['comments']), 1)
        self.assertTrue(assessments[0]['can_continue'])
        self.assertEqual(assessments[0]['statistics']['pending_comment_tasks'], 1)
        self.assertEqual(assessments[-1]['statistics']['valid_comment_count'], 1)
        self.assertFalse(assessments[-1]['can_continue'])

    def test_missing_comments_does_not_override_model_refinement(self):
        class Provider(SocialProvider):
            def json(inner, role, instruction, data):
                if '"covered"' in instruction:
                    return dict(covered=False,action='refine',reason='需找有评论的原始帖子',evidence_ids=[],next_query='故障 原始帖子')
                return super().json(role,instruction,data)
        self.service.provider = Provider()
        task = self.collector.task(dict(platform='wb',query='故障',comments=True))
        self.collector.records.append(task)
        self.collector.finish_task(task, 'exhausted')
        self.collector.decide(task)
        self.assertEqual(task['suggested_query'], '故障 原始帖子')

    def test_comment_only_resume_refreshes_need_and_final_assessment(self):
        parent = self.collector.task(dict(platform='wb',query='故障',comments=True,question='旧问题'))
        self.collector.finish_task(parent, 'exhausted')
        parent['_terminal_assessed'] = True
        self.service.store.save_collection(self.mid, parent)
        child = dict(parent, id='child', parent_task=parent['id'],kind='comments',post={'id':'p1'},status='timeout',resumable=True)
        self.service.store.save_collection(self.mid, child)
        resumed = self.collector.task(dict(platform='wb',query='故障',comments=True,question='核查评论范围'))
        self.assertEqual(resumed['id'],parent['id'])
        self.assertEqual(resumed['status'],'exhausted')
        self.assertEqual(resumed['question'],'核查评论范围')
        self.assertNotIn('_terminal_assessed', resumed)

    def test_user_stop_and_empty_duplicates_filtered_are_distinct(self):
        self.service.stop_collection(self.mid)
        self.service._search_unbudgeted = lambda *args: self.fail('must not request')
        self.assertEqual(self.collector.search('补证'), [])
        outcome = supplement_outcome(self.collector.records[-1], [])
        self.assertEqual(outcome['status'], 'user_stopped')
        self.assertEqual(outcome['request_count'], 0)
        self.assertIn('未发起', outcome['message'])
        for fields, status in [({'raw_count':0}, 'no_results'), ({'duplicates':4}, 'duplicates'), ({'excluded':3}, 'filtered')]:
            self.assertEqual(supplement_outcome(dict(status='complete', request_count=1, **fields), [])['status'], status)

    def test_legacy_supplement_labels_use_logs_without_mutating_history(self):
        records = [dict(query='补证', status='empty', message='新增 0 条相关证据', new_evidence_ids=[])]
        before = copy.deepcopy(records)
        collections = [dict(id='search-1', query='补证', platform='web', status='budget_exhausted', request_count=0)]
        corrected = explain_supplements(records, collections)
        self.assertEqual(records, before)
        self.assertEqual(corrected[0]['status'], 'budget_exhausted')
        self.assertEqual(corrected[0]['collection_task_id'], 'search-1')

    def test_malformed_coverage_cannot_close_requirement(self):
        class Provider(SocialProvider):
            def json(self, role, instruction, data):
                if '"covered"' in instruction:
                    return dict(covered=True, evidence_ids=['E-000000000000'], reason='错误引用')
                return super().json(role,instruction,data)
        self.service.provider = Provider()
        records = self.run_pages(Pages())
        self.assertTrue(all(r['assessment']['status']!='covered' for r in records))

    def test_storage_over_old_limits_pagination_and_frozen_snapshot(self):
        evidence = [make_evidence('故障资料 '+str(i), '资料'+str(i), '段落', source_id=str(i)) for i in range(1605)]
        comments = [dict(id=str(i), text='评论 '+str(i), source='微博') for i in range(1105)]
        snapshot = dict(id='snapshot-test', round=1, created_at='today', summary={}, evidence=copy.deepcopy(evidence))
        self.service.store.change(self.mid, lambda s:s.update(evidence=evidence, comments=comments, snapshots=[snapshot]))
        page = self.client.get(f'/api/discussions/{self.mid}/evidence?page=53').json
        self.assertEqual(page['total'], 1605)
        self.assertEqual(len(page['items']), 15)
        public = self.client.get(f'/api/discussions/{self.mid}').json
        self.assertEqual(public['comment_count'], 1105)
        self.assertEqual(len(public['evidence']), 30)
        with self.service.store.connect() as conn:
            packed = json.loads(conn.execute('SELECT state FROM meetings WHERE id=?',(self.mid,)).fetchone()[0])
        self.assertNotIn('evidence', packed)
        self.assertNotIn('comments', packed)
        self.assertNotIn('evidence', packed['snapshots'][0])
        self.service.store.change(self.mid, lambda s:s['evidence'][0].update(text='修订资料'))
        self.assertEqual(self.state(self.mid)['snapshots'][0], snapshot)
        self.assertEqual(self.client.get(f'/api/discussions/{self.mid}/evidence/{evidence[-1]["id"]}').status_code,200)

    def test_sampling_reproducible_cached_and_not_generalized(self):
        class Provider(FakeProvider):
            calls=0
            def json(self, *args):
                self.calls+=1
                return super().json(*args)
        provider=Provider()
        comments=[dict(id=str(i),text='故障评论 '+str(i), platform='wb' if i%2 else 'xhs', source='测试', time='2026-11-17',parent_id=str(i%6)) for i in range(800)]
        a=classify(self.service.store,provider,comments,500,'same-seed')
        calls=provider.calls
        b=classify(self.service.store,provider,list(reversed(comments)),500,'same-seed')
        self.assertEqual(a['sampling'],b['sampling'])
        self.assertEqual(provider.calls,calls)
        self.assertEqual(a['sampled'],500)
        self.assertEqual(a['classified'],500)
        self.assertEqual(a['percentages']['负面'],100)
        self.assertIn('不推断全部',a['scope'])
        self.assertEqual(sum(a['sampling']['allocations'].values()),500)

    def test_source_settings_and_resume_validation(self):
        self.assertEqual(self.client.post(f'/api/discussions/{self.mid}/collections/resume',json={'task_ids':['alien']}).status_code,409)
        self.service.store.change(self.mid,lambda s:s.update(status='waiting',operation=None))
        self.assertEqual(self.client.post(f'/api/discussions/{self.mid}/collections/resume',json={'task_ids':['alien']}).status_code,400)
        self.assertEqual(self.client.patch(f'/api/discussions/{self.mid}/sources',json={'crawl_platforms':[],'sentiment_sample_size':0}).status_code,400)

    def test_source_changes_keep_id_and_old_snapshot_version(self):
        self.run_pages(Pages(pages=1))
        original=copy.deepcopy(self.state(self.mid)['evidence'])
        snap=dict(id='version-before',round=1,created_at='before',summary={},evidence=original)
        self.service.store.change(self.mid,lambda s:s['snapshots'].append(snap))
        changed,_=normalize(dict(posts=[dict(id='p1_0',text='更正后的故障资料')]),'wb','故障事件','later')
        task=self.collector.records[0]
        self.collector.begin()
        self.collector.ingest(task,changed,[])
        self.collector.end()
        state=self.state(self.mid)
        self.assertEqual(len(state['evidence']),16)
        before = next(e for e in original if e['source_id']=='wb:post:p1_0')
        after = next(e for e in state['evidence'] if e['source_id']=='wb:post:p1_0')
        self.assertEqual(after['id'],before['id'])
        self.assertEqual(after['text'],'更正后的故障资料')
        self.assertEqual(state['snapshots'][0]['evidence'],original)

    def test_failed_detail_summary_cannot_replace_stored_full_text(self):
        self.run_pages(Pages(pages=1))
        before=next(e for e in self.state(self.mid)['evidence'] if e['source_id']=='wb:post:p1_0')
        shorter,_=normalize(dict(posts=[dict(id='p1_0',text='摘要',content_scope='search_summary')]),'wb','故障事件','later')
        self.collector.ingest(self.collector.records[0],shorter,[])
        after=next(e for e in self.state(self.mid)['evidence'] if e['source_id']=='wb:post:p1_0')
        self.assertEqual(after['text'],before['text'])
        self.assertEqual(after['id'],before['id'])

    def test_web_relevance_batch_failure_keeps_successful_materials(self):
        evidence=[make_evidence('故障原始材料 '+str(i),str(i),'摘要') for i in range(12)]
        class Provider(SocialProvider):
            calls=0
            def search(inner,*args): return evidence
            def json(inner,role,instruction,data):
                inner.calls+=1
                if inner.calls==2: raise TimeoutError()
                return dict(accepted_ids=[e['id'] for e in data['evidence']])
        self.service.provider=Provider()
        diagnostics={}
        result=self.service._search_unbudgeted('故障',self.s,[],diagnostics)
        self.assertEqual(len(result),8)
        self.assertTrue(diagnostics['pending_relevance'])
        self.assertEqual(diagnostics['excluded'],0)
        self.assertEqual(diagnostics['raw_count'],12)

    def test_comment_resume_does_not_repeat_search(self):
        self.run_pages(Pages(pages=1))
        parent=self.collector.records[0]
        post=dict(id='p1_0',text='故障事件材料1_0')
        child=dict(parent,id='comment-task',kind='comments',parent_task=parent['id'],post=post,
                   cursor={'next':18,'page':1},status='budget_exhausted',resumable=True)
        self.service.store.save_collection(self.mid,child)
        class Crawler:
            calls=[]
            def collect_page(inner,mid,n,task,remaining,stopped):
                inner.calls.append((task['kind'],task['cursor']['next']))
                raw=dict(status='complete',parents=[post],posts=[],comments=[dict(id='c19',parent_id='p1_0',text='故障评论')],
                         has_more=False,page=2,cursor={'next':0,'page':2},request_count=1)
                evidence,comments=normalize(raw,'wb','故障事件','today')
                return raw,evidence,comments
        self.service.crawler=Crawler()
        collector=RoundCollector(self.service,self.state(self.mid),self.s['operation'],copy.deepcopy(self.state(self.mid)['evidence']),[])
        collector.run(resume_ids=['comment-task'])
        self.assertEqual(self.service.crawler.calls,[('comments',18)])
        self.assertEqual(self.state(self.mid)['comments'][0]['id'],'wb:comment:c19')

    def test_invalid_cursor_restarts_once_and_records_reason(self):
        class Crawler(Pages):
            invalid=True
            def collect_page(inner,*args):
                if inner.invalid and args[2]['platform']=='wb':
                    inner.invalid=False
                    return dict(status='cursor_invalid',posts=[],comments=[],request_count=1),[],[]
                return super().collect_page(*args)
        self.run_pages(Crawler(pages=1))
        self.assertEqual(len(self.collector.records[0]['cursor_resets']),1)
        self.assertEqual(len(self.state(self.mid)['evidence']),16)

    def test_failed_labels_denominator_unknown_strata_and_empty(self):
        class Provider(FakeProvider):
            def json(self,role,instruction,data):
                return dict(labels=[dict(id=data['comments'][0]['id'],label='中性')])
        comments=[dict(id=str(i),text='故障 '+str(i),source='测试',time='' if i%2 else '2026-11-17') for i in range(8)]
        result=classify(self.service.store,Provider(),comments,8,'fail-seed')
        self.assertEqual(result['classified'],1)
        self.assertEqual(result['failed'],7)
        self.assertEqual(result['percentages']['中性'],100)
        self.assertTrue(any('日期未知' in k for k in result['sampling']['allocations']))
        self.assertTrue(all(v is None for v in classify(self.service.store,Provider(),[],8)['percentages'].values()))


class MigrationTests(unittest.TestCase):
    def test_legacy_backup_and_snapshot_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'discussions.sqlite3'
            evidence=[make_evidence('旧原文','旧资料','段落')]
            original=dict(id='meeting-old',title='旧会议',mode='live',status='waiting',updated_at='old',evidence=evidence,comments=[],snapshots=[dict(id='snapshot-old',round=1,created_at='old',summary={},evidence=evidence)])
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE meetings(id TEXT PRIMARY KEY,state TEXT,updated TEXT)')
                conn.execute('INSERT INTO meetings VALUES(?,?,?)',('meeting-old',json.dumps(original),'old'))
            conn.close()
            store=Store(tmp)
            self.assertTrue((Path(tmp)/'discussions.before-collection-v3.sqlite3').exists())
            self.assertEqual(store.get('meeting-old')['snapshots'],original['snapshots'])
            again=Store(tmp)
            self.assertEqual(again.evidence_page('meeting-old')['total'],1)

    def test_worker_xhs_page_cursor_and_comment_cursor(self):
        class Client:
            def __init__(self): self.calls=[]
            async def get_note_by_keyword(self,q,**kw):
                self.calls.append(kw)
                return dict(has_more=kw['page']==1,items=[dict(id=f"p{kw['page']}_{i}") for i in range(9)])
            async def get_note_by_id(self,pid,source,token): return dict(note_id=pid,desc='故障记录')
            async def get_note_comments(self,pid,token,cursor):
                self.calls.append(cursor)
                return dict(has_more=not bool(cursor),cursor='next',comments=[dict(id=cursor+str(i),content='评论') for i in range(15)])
        with tempfile.TemporaryDirectory() as tmp:
            client=Client()
            spec=dict(platform='xhs',query='故障事件',kind='search',cursor={})
            first=dict(posts=[],comments=[])
            asyncio.run(collect(client,spec,first,Path(tmp)/'result.json'))
            spec['cursor']=first['cursor']
            second=dict(posts=[],comments=[])
            asyncio.run(collect(client,spec,second,Path(tmp)/'result.json'))
            self.assertEqual(len(first['posts'])+len(second['posts']),18)
            self.assertFalse(second['has_more'])
            self.assertEqual(client.calls[0]['search_id'],client.calls[1]['search_id'])
            spec.update(kind='comments',post=first['posts'][0],cursor={})
            result=dict(posts=[],comments=[])
            asyncio.run(collect(client,spec,result,Path(tmp)/'result.json'))
            self.assertEqual(len(result['comments']),15)
            self.assertEqual(result['cursor']['next'],'next')

    def test_worker_weibo_search_and_comment_pagination(self):
        class Client:
            calls=[]
            async def get_note_by_keyword(self,q,page):
                return dict(cards=[dict(mblog=dict(id=f'{page}_{i}',text='正文')) for i in range(7)] if page<3 else [])
            async def get_note_comments(self,pid,next_cursor,max_id_type):
                self.calls.append((next_cursor,max_id_type))
                return dict(data=[dict(id=str(next_cursor)+'c',text='评论')],max_id=19 if next_cursor==0 else 0,max_id_type=1)
        with tempfile.TemporaryDirectory() as tmp:
            client=Client()
            spec=dict(platform='wb',query='事件',kind='search',cursor={})
            posts=[]
            for _ in range(3):
                result=dict(posts=[],comments=[])
                asyncio.run(collect(client,spec,result,Path(tmp)/'result.json'))
                posts.extend(result['posts'])
                spec['cursor']=result['cursor']
            self.assertEqual(len(posts),14)
            self.assertFalse(result['has_more'])
            spec.update(kind='comments',post=posts[0],cursor={})
            for _ in range(2):
                result=dict(posts=[],comments=[])
                asyncio.run(collect(client,spec,result,Path(tmp)/'result.json'))
                spec['cursor']=result['cursor']
            self.assertEqual(client.calls,[(0,0),(19,1)])
            self.assertFalse(result['has_more'])

    def test_worker_douyin_offsets_search_id_and_comments(self):
        var=types.ModuleType('var')
        var.request_keyword_var=ContextVar('test-keyword')
        field=types.ModuleType('media_platform.douyin.field')
        field.SearchChannelType=types.SimpleNamespace(VIDEO='video')
        class Client:
            calls=[]
            async def search_info_by_keyword(self,q,**kw):
                self.calls.append(kw)
                offset=kw['offset']
                return dict(data=[dict(aweme_info=dict(aweme_id=f'{offset}_{i}',desc='正文')) for i in range(8)],cursor=offset+8,has_more=offset==0,log_pb={'impr_id':'search-session'})
            async def get_aweme_comments(self,pid,cursor):
                return dict(comments=[dict(cid=str(cursor),text='评论')],cursor=cursor+20,has_more=cursor==0)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules,{'var':var,'media_platform.douyin.field':field}):
            client=Client()
            spec=dict(platform='dy',query='事件',kind='search',cursor={})
            posts=[]
            for _ in range(2):
                result=dict(posts=[],comments=[])
                asyncio.run(collect(client,spec,result,Path(tmp)/'result.json'))
                posts.extend(result['posts'])
                spec['cursor']=result['cursor']
            self.assertEqual(len(posts),16)
            self.assertEqual(client.calls[1]['offset'],8)
            self.assertEqual(client.calls[1]['search_id'],'search-session')
            self.assertFalse(result['has_more'])
            spec.update(kind='comments',post=posts[0],cursor={})
            for _ in range(2):
                result=dict(posts=[],comments=[])
                asyncio.run(collect(client,spec,result,Path(tmp)/'result.json'))
                spec['cursor']=result['cursor']
            self.assertEqual(result['cursor']['next'],40)
            self.assertFalse(result['has_more'])


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    names=[name for name in CollectionTests.__dict__ if name.startswith('test_')]
    suite.addTests(CollectionTests(name) for name in names)
    suite.addTests(loader.loadTestsFromTestCase(MigrationTests))
    return suite


if __name__=='__main__':
    unittest.main(verbosity=2)
