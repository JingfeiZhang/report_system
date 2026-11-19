"""Regression cases for comment intent, old-post backfill and honest pagination."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_discussion_collection import CollectionTests, Pages
from test_discussion_crawler import SocialProvider
from discussion.collection import RoundCollector
from discussion.crawler import normalize
from discussion.crawler_worker import collect, request_once, UnexpectedResponse, AccessRequired, ContentUnavailable


class CommentAcquisitionTests(CollectionTests):
    def test_new_sentiment_role_is_not_discarded_by_initial_fact_roles(self):
        class Provider(SocialProvider):
            def json(self, role, instruction, data):
                if '"tasks"' in instruction:
                    return {'tasks': [{'role': 'sentiment', 'question': '核查网友诉求并补充评论样本', 'feedback_ids': ['m1']}]}
                return super().json(role, instruction, data)
        self.service.provider = Provider()
        state = self.state(self.mid)
        self.assertNotIn('sentiment', state['roles'])
        tasks = self.service._tasks(state, [dict(id='m1', target='all', content='多看看不同帖子的评论')])
        self.assertEqual(tasks[0]['role'], 'sentiment')
        self.assertTrue(tasks[0]['needs_comments'])
        plans = self.collector.plan(tasks, [])
        self.assertEqual({p['platform'] for p in plans if p['comments']}, {'wb', 'xhs'})
        self.assertTrue(all('评论原文' in p['condition'] for p in plans))

    def test_collector_can_find_comment_need_independently_of_roles(self):
        class Provider(SocialProvider):
            def json(self, role, instruction, data):
                result = super().json(role, instruction, data)
                if '"collections"' in instruction:
                    result['comment_requirement'] = dict(required=True, question='补充两个平台不同帖子的评论样本')
                return result
        self.service.provider = Provider()
        plans = self.collector.plan([dict(role='facts', question='核查事件经过')], [dict(target='all', content='也看看网友怎么说')])
        self.assertTrue(all(p['comments'] for p in plans))
        self.assertTrue(all('不同帖子' in p['comment_question'] for p in plans))

    def test_fact_only_does_not_force_comment_collection(self):
        plans = self.collector.plan([dict(role='facts', question='核查原始通报日期')], [])
        self.assertFalse(any(p.get('comments') for p in plans))

    def test_saved_search_pages_backfill_comments_without_revisiting_old_page(self):
        root = self.collector.task(dict(platform='xhs', query='故障事件', comments=False))
        post = dict(id='old-post', text='故障事件详情', token='PRIVATE-TOKEN')
        raw = dict(status='complete', posts=[post], comments=[], has_more=True,
                   cursor={'page': 1, 'next': 2}, page=1)
        folder = self.service.store.root / self.mid / 'crawls' / 'saved-page'
        folder.mkdir(parents=True)
        (folder / 'result.json').write_text(json.dumps(raw), encoding='utf-8')
        evidence, comments = normalize(raw, 'xhs', root['query'], 'today')
        self.collector.ingest(root, evidence, comments)
        root.update(cursor=raw['cursor'], raw_pages=[str(folder.relative_to(self.service.store.root))],
                    resumable=True, status='interrupted', page=1)
        self.service.store.save_collection(self.mid, root)
        class Crawler:
            calls = []
            def collect_page(inner, mid, number, task, remaining, stopped):
                inner.calls.append((task['kind'], task['cursor'].get('page', 0)))
                if task['kind'] == 'comments':
                    self.assertEqual(task['post']['token'], 'PRIVATE-TOKEN')
                    raw = dict(status='complete', posts=[], parents=[post], comments=[dict(id='c-old', parent_id=post['id'], text='故障仍未修好')], has_more=False, request_count=1)
                else:
                    self.assertEqual(task['cursor']['next'], 2)
                    raw = dict(status='complete', posts=[], comments=[], has_more=False, request_count=1)
                found, rows = normalize(raw, 'xhs', '故障事件', 'today')
                return raw, found, rows
        self.service.crawler = Crawler()
        state = self.state(self.mid)
        collector = RoundCollector(self.service, state, state['operation'], copy.deepcopy(state['evidence']), [])
        collector.run(plans=[dict(platform='xhs', query='故障事件', comments=True)])
        self.assertEqual(set(self.service.crawler.calls), {('search', 1), ('comments', 0)})
        self.assertEqual(len(self.state(self.mid)['comments']), 1)
        self.assertNotIn('PRIVATE-TOKEN', json.dumps(self.service.public(self.state(self.mid))))

    def test_comment_coverage_requires_actual_comment_citation(self):
        root = self.collector.task(dict(platform='wb', query='故障事件', comments=True))
        post = dict(id='p1', text='故障事件原帖')
        raw = dict(posts=[post], comments=[dict(id=str(i), parent_id='p1', text='故障评论'+str(i), date='2026-11-17') for i in range(3)])
        evidence, comments = normalize(raw, 'wb', root['query'], 'today')
        self.collector.ingest(root, evidence, comments)
        self.collector.records.append(root)
        class Provider(SocialProvider):
            def json(inner, role, instruction, data):
                if '"covered"' in instruction:
                    self.assertEqual(data['statistics']['comment_coverage']['post_count'], 1)
                    self.assertEqual(data['statistics']['comment_coverage']['largest_post_comments'], 3)
                    self.assertTrue(any(e['source_type'] == 'social_comment' for e in data['evidence']))
                    return dict(covered=True, action='stop', reason='找到帖子', evidence_ids=[evidence[0]['id']])
                return super().json(role, instruction, data)
        self.service.provider = Provider()
        self.collector.decide(root, can_continue=True)
        self.assertEqual(root['assessment']['status'], 'gaps')
        self.assertEqual(root['assessment']['action'], 'continue')

    def test_one_comment_thread_failure_keeps_other_posts_and_platforms(self):
        class Crawler:
            calls = []
            def collect_page(inner, mid, number, task, remaining, stopped):
                platform = task['platform']
                pid = task.get('post', {}).get('id')
                inner.calls.append((platform, task['kind'], pid))
                posts = [dict(id='p1', text='故障资料1'), dict(id='p2', text='故障资料2')]
                raw = dict(status='complete', posts=posts if task['kind']=='search' else [],
                           parents=[task['post']] if pid else [], comments=[], has_more=False, request_count=1)
                if pid == 'p1' and platform == 'wb':
                    raw.update(status='content_unavailable', message='评论关闭')
                elif pid:
                    raw['comments'] = [dict(id=pid+'-c', parent_id=pid, text='故障评论'+pid)]
                found, rows = normalize(raw, platform, task['query'], 'today')
                return raw, found, rows
        self.service.crawler = Crawler()
        self.collector.run(plans=[dict(platform=p, query='故障事件', comments=True) for p in ['wb', 'xhs']])
        self.assertEqual(len(self.state(self.mid)['comments']), 3)
        self.assertFalse(self.collector.blocked_platforms)
        failed = next(t for t in self.collector.records if t['status']=='content_unavailable')
        self.assertFalse(failed['resumable'])


class CommentResponseTests(unittest.TestCase):
    def test_string_zero_is_last_page_and_missing_fields_are_not_exhaustion(self):
        class Client:
            response = {}
            async def get_note_comments(self, *args, **kwargs): return self.response
        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            spec = dict(platform='wb', query='事件', kind='comments', post={'id':'p'}, cursor={})
            for malformed in ({}, {'data': []}, {'data': 'bad', 'max_id':0}):
                client.response = malformed
                with self.assertRaises(UnexpectedResponse):
                    asyncio.run(collect(client, spec, dict(posts=[], comments=[]), Path(tmp)/'out.json'))
            client.response = dict(data=[dict(id='last', text='最后一条')], max_id='0')
            result = dict(posts=[], comments=[])
            asyncio.run(collect(client, spec, result, Path(tmp)/'out.json'))
            self.assertFalse(result['has_more'])
            self.assertEqual(len(result['comments']), 1)

    def test_xhs_invalid_pagination_preserves_cursor(self):
        class Client:
            async def get_note_comments(self, *args, **kwargs):
                return dict(comments=[], has_more=True)
        with tempfile.TemporaryDirectory() as tmp:
            spec = dict(platform='xhs', query='事件', kind='comments', post={'id':'p'}, cursor={'next':'saved'})
            with self.assertRaises(UnexpectedResponse):
                asyncio.run(collect(Client(), spec, dict(posts=[], comments=[]), Path(tmp)/'out.json'))
            self.assertEqual(spec['cursor'], {'next':'saved'})

    def test_business_error_does_not_masquerade_as_expired_login(self):
        import httpx
        with tempfile.TemporaryDirectory() as tmp:
            for platform, payload, error in [
                ('wb', dict(ok=0, msg='请求失败'), UnexpectedResponse),
                ('wb', dict(ok=0, msg='请先登录'), AccessRequired),
                ('wb', dict(ok=0, msg='评论已关闭'), ContentUnavailable),
                ('xhs', dict(success=False, code=999, msg='系统异常'), UnexpectedResponse),
            ]:
                client = SimpleNamespace(discussion_platform=platform, discussion_result={}, discussion_output=Path(tmp)/'out.json')
                http = AsyncMock()
                http.request.return_value = httpx.Response(200, json=payload, request=httpx.Request('GET','https://example.com/comments'))
                with patch('httpx.AsyncClient') as factory, patch('discussion.crawler_worker.asyncio.sleep', new_callable=AsyncMock):
                    factory.return_value.__aenter__.return_value = http
                    with self.assertRaises(error):
                        asyncio.run(request_once(client, 'GET', 'https://example.com/comments'))

    def test_weibo_no_comments_message_is_empty_page_not_login_failure(self):
        import httpx
        with tempfile.TemporaryDirectory() as tmp:
            client = SimpleNamespace(discussion_platform='wb', discussion_result={}, discussion_output=Path(tmp)/'out.json')
            http = AsyncMock()
            http.request.return_value = httpx.Response(200, json=dict(ok=0, msg='还没有人评论哦~快来抢沙发！'),
                                                       request=httpx.Request('GET','https://m.weibo.cn/comments/hotflow'))
            with patch('httpx.AsyncClient') as factory, patch('discussion.crawler_worker.asyncio.sleep', new_callable=AsyncMock):
                factory.return_value.__aenter__.return_value = http
                response = asyncio.run(request_once(client, 'GET', 'https://m.weibo.cn/comments/hotflow'))
            self.assertEqual(response['data'], [])
            self.assertEqual(response['max_id'], 0)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(CommentAcquisitionTests(name) for name in CommentAcquisitionTests.__dict__ if name.startswith('test_'))
    suite.addTests(loader.loadTestsFromTestCase(CommentResponseTests))
    return suite


if __name__ == '__main__':
    unittest.main(verbosity=2)
