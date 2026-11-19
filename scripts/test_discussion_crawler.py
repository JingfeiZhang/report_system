"""Crawler integration tests with synthetic responses; no platform or paid calls."""
import asyncio
import copy
import json
import os
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_discussion import DiscussionTests, FakeProvider
from discussion.crawler import CrawlerBridge, normalize, platforms
from discussion.crawler_worker import (collect, check_stop, CollectionStopped, RateLimited,
                                       request_once, UnexpectedResponse, AccessRequired)


def sample():
    return {"posts": [{"id": "post1", "text": "服务中断，平台回应正在修复", "title": "故障说明", "date": "2026-11-17"}],
            "comments": [{"id": "comment1", "parent_id": "post1", "text": "服务太差，无法使用", "date": "2026-11-17"}]}


class SocialProvider(FakeProvider):
    def json(self, role, instruction, data):
        if '"accepted_ids"' in instruction:
            return {'accepted_ids': [e['id'] for e in data['evidence']]}
        if '"covered"' in instruction:
            return {'covered': False, 'evidence_ids': [], 'reason': '缺少官方材料', 'gaps': []}
        if '"collections"' in instruction:
            return {"collections": [{"platform": p, "query": data["title"], "reason": "回应本轮追问"} for p in data["selected_platforms"]]}
        return super().json(role, instruction, data)


class FakeCrawler:
    def __init__(self):
        self.calls, self.hook, self.fail = [], None, False

    def collect_page(self, mid, number, task, remaining, stopped=lambda: False):
        record, evidence, comments = self.collect(mid, number, task['platform'], task['query'])
        record.update(has_more=False, cursor={'next':2,'page':1}, page=1, request_count=1, posts=[], comments=[])
        return record, evidence, comments

    def collect(self, mid, number, platform, query):
        self.calls.append((mid, number, platform, query))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        evidence, comments = normalize(sample(), platform, query, "2026-11-17")
        if self.fail:
            evidence, comments = [], []
        record = {"platform": platform, "platform_name": "抖音", "query": query,
                  "status": "needs_login" if self.fail else "complete", "message": "需要登录" if self.fail else "采集完成",
                  "post_count": int(not self.fail), "comment_count": int(not self.fail)}
        return record, evidence, comments


class CrawlerFlowTests(DiscussionTests):
    # Inherit setup helpers, but execute only the crawler-specific scenarios below.
    def setUp(self):
        super().setUp()
        self.service.provider = SocialProvider()
        self.crawler = FakeCrawler()
        self.service.crawler = self.crawler

    def enable(self, mid):
        response = self.client.patch(f'/api/discussions/{mid}/sources', json={"crawl_platforms": ["dy", "xhs"]})
        self.assertEqual(response.status_code, 200)

    def test_crawler_round_feedback_snapshot_and_report(self):
        mid = self.create("live")
        self.enable(mid)
        self.start(mid)
        first = self.state(mid)
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(len(first["rounds"][0]["collections"]), 2)
        self.assertEqual(first["rounds"][0]["sentiment"]["total"], 2)
        self.assertEqual(first["rounds"][0]["sentiment"]["unique"], 1)
        self.assertTrue(any(c["role"] == "sentiment" for c in first["rounds"][0]["cards"]))
        self.client.post(f'/api/discussions/{mid}/messages', json={"content": "继续核查评论", "target": "sentiment"})
        self.start(mid)
        second = self.state(mid)
        self.assertEqual(second["snapshots"][0], first["snapshots"][0])
        self.assertEqual(len(second["comments"]), 2)
        self.assertEqual(len(second["evidence"]), 4)
        self.assertEqual(second["messages"][-1]["status"], "answered")
        self.assertEqual(len(self.crawler.calls), 4)
        self.assertEqual(second["rounds"][1]["collections"][0]["new_comment_count"], 0)
        snapshot = second["snapshots"][-1]
        self.client.post(f'/api/discussions/{mid}/reports', json={"snapshot_id": snapshot["id"]})
        self.assertEqual(self.state(mid)["reports"][-1]["status"], "complete")
        other = self.create("live")
        self.assertFalse(self.state(other)["evidence"])

    def test_crawler_frozen_upload_and_message(self):
        mid = self.create("live")
        self.enable(mid)
        def concurrent():
            self.service.upload(mid, "later.txt", "下一轮才能使用的新资料".encode())
            self.service.message(mid, {"content": "这条也是下一轮指导"})
        self.crawler.hook = concurrent
        self.start(mid)
        state = self.state(mid)
        snapshot = state["snapshots"][-1]
        self.assertEqual(len(state["evidence"]), 5)
        self.assertEqual(len(snapshot["evidence"]), 4)
        self.assertEqual(state["messages"][-1]["status"], "pending")

    def test_crawler_login_failure_is_gap_no_statistics(self):
        mid = self.create("live")
        self.enable(mid)
        self.crawler.fail = True
        self.start(mid)
        state = self.state(mid)
        self.assertEqual(state["status"], "waiting")
        self.assertIsNone(state["snapshots"][0]["sentiment"])
        self.assertFalse(state["evidence"])
        self.assertIn("需要登录", " ".join(state["rounds"][0]["summary"]["gaps"]))

    def test_crawler_platform_validation_and_login_origin(self):
        mid = self.create("live")
        self.assertEqual(self.client.patch(f'/api/discussions/{mid}/sources', json={"crawl_platforms": ["../../bad"]}).status_code, 400)
        self.assertEqual(self.client.post('/api/discussion-crawler/dy/login', json={}, headers={"Origin": "https://untrusted.example"}).status_code, 403)
        self.assertEqual(self.client.post('/api/discussion-crawler/dy/login', json={}, environ_base={"REMOTE_ADDR": "192.0.2.1"}).status_code, 403)
        self.service.store.change(mid, lambda s: s.update(status="running"))
        self.assertEqual(self.client.patch(f'/api/discussions/{mid}/sources', json={"crawl_platforms": ["dy"]}).status_code, 409)


class WorkerTests(unittest.TestCase):
    def test_douyin_video_uses_video_endpoint(self):
        code = '''
import asyncio
from discussion.crawler_worker import load_client
cls = load_client('dy')
from media_platform.douyin.field import SearchChannelType
client = cls.__new__(cls)
client.headers = {}
calls = []
async def get(uri, params, headers):
    calls.append((uri, params, headers))
    return {}
client.get = get
asyncio.run(client.search_info_by_keyword('事件', search_channel=SearchChannelType.VIDEO))
assert calls[-1][0] == '/aweme/v1/web/search/item/'
assert 'type=video' in calls[-1][2]['Referer']
assert 'from_group_id' not in calls[-1][1]
asyncio.run(client.search_info_by_keyword('事件'))
assert calls[-1][0] == '/aweme/v1/web/general/search/single/'
asyncio.run(client.get_aweme_comments('123', cursor=20))
assert calls[-1][0] == '/aweme/v1/web/comment/list/'
assert calls[-1][1]['cursor'] == 20
assert calls[-1][2]['Referer'] == 'https://www.douyin.com/video/123'
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_douyin_search_schemas_and_last_page(self):
        code = '''
import asyncio, tempfile
from pathlib import Path
from discussion.crawler_worker import collect, load_client, UnexpectedResponse
load_client('dy')
class Client:
    async def search_info_by_keyword(self, query, **kwargs):
        assert kwargs['offset'] == 15
        return self.response
client=Client()
with tempfile.TemporaryDirectory() as tmp:
    for key,wrapped in [('data',True),('data',False),('aweme_list',False)]:
        posts=[dict(aweme_id=str(i),desc='测试正文') for i in range(18)]
        client.response={key:[{'aweme_info':p} for p in posts] if wrapped else posts,
                         'cursor':33,'has_more':0,'log_pb':{'impr_id':'next-search'}}
        result=dict(posts=[],comments=[])
        asyncio.run(collect(client,dict(platform='dy',query='测试',cursor={'next':15,'page':1}),result,Path(tmp)/'result.json'))
        assert len(result['posts']) == 18 and result['status'] == 'complete'
        assert result['has_more'] is False and result['page'] == 2
        assert result['cursor']['next'] == 33 and result['cursor']['search_id'] == 'next-search'
    client.response={'status_code':0}
    try:
        asyncio.run(collect(client,dict(platform='dy',query='测试',cursor={'next':15}),dict(posts=[],comments=[]),Path(tmp)/'result.json'))
    except UnexpectedResponse:
        pass
    else:
        raise AssertionError('missing data must not count as exhausted or login failure')
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_http_errors_distinguish_auth_rate_limit_and_invalid_body(self):
        import httpx
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            for status, body, expected in [(200,b'',UnexpectedResponse), (200,b'<html>error</html>',UnexpectedResponse),
                                            (403,b'',AccessRequired),(429,b'',RateLimited)]:
                client=SimpleNamespace(discussion_platform='dy',discussion_result={},discussion_output=Path(tmp)/'result.json')
                response=httpx.Response(status,content=body,headers={'content-type':'text/plain'},request=httpx.Request('GET','https://www.douyin.com/api'))
                http=AsyncMock()
                http.request.return_value=response
                with patch('httpx.AsyncClient') as factory, patch('discussion.crawler_worker.asyncio.sleep',new_callable=AsyncMock):
                    factory.return_value.__aenter__.return_value=http
                    with self.assertRaises(expected):
                        asyncio.run(request_once(client,'GET','https://www.douyin.com/api?token=PRIVATE',headers={'Cookie':'PRIVATE'}))
                self.assertEqual(client.discussion_result['request_count'],1)
                self.assertEqual(client.discussion_result['response_diagnostic']['http_status'],status)
                self.assertNotIn('PRIVATE',json.dumps(client.discussion_result))

    def test_old_deadline_is_ignored_but_stop_file_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Client: pass
            client = Client()
            client.discussion_deadline = 0
            client.discussion_stop = Path(tmp)/'stop'
            check_stop(client)
            client.discussion_stop.touch()
            with self.assertRaises(CollectionStopped):
                check_stop(client)

    def test_xhs_detail_failure_keeps_summary_and_continues(self):
        class Client:
            async def get_note_by_keyword(self,q,**kw):
                return dict(has_more=False, items=[dict(id=str(i),note_card={'display_title':'故障摘要'}) for i in range(3)])
            async def get_note_by_id(self,pid,*args):
                if pid=='1':
                    raise TimeoutError()
                return dict(note_id=pid,desc='完整正文')
        with tempfile.TemporaryDirectory() as tmp:
            result=dict(posts=[],comments=[])
            asyncio.run(collect(Client(),dict(platform='xhs',query='事件'),result,Path(tmp)/'result.json'))
        self.assertEqual([p['id'] for p in result['posts']], ['0','1','2'])
        self.assertEqual(result['posts'][1]['content_scope'],'search_summary')
        self.assertEqual(result['item_failures'][0]['error_type'],'TimeoutError')
        self.assertEqual(result['status'],'complete')

    def test_xhs_access_limit_preserves_pending_details(self):
        class Client:
            async def get_note_by_keyword(self,q,**kw):
                return dict(has_more=False,items=[dict(id=str(i)) for i in range(3)])
            async def get_note_by_id(self,pid,*args):
                if pid=='1': raise RateLimited()
                return dict(note_id=pid,desc='正文')
        with tempfile.TemporaryDirectory() as tmp:
            result=dict(posts=[],comments=[])
            with self.assertRaises(RateLimited):
                asyncio.run(collect(Client(),dict(platform='xhs',query='事件'),result,Path(tmp)/'result.json'))
        self.assertEqual(len(result['posts']),1)
        self.assertEqual([p['id'] for p in result['cursor']['pending']],['1','2'])

    def test_weibo_long_post_fetches_full_text(self):
        class Client:
            async def get_note_by_keyword(self,q,**kw):
                return dict(cards=[dict(mblog=dict(id='p1',text='截断内容',isLongText=True))])
            async def get(self,uri,params):
                self.uri=uri
                return dict(longTextContent='原帖的完整长正文。')
        with tempfile.TemporaryDirectory() as tmp:
            client=Client();result=dict(posts=[],comments=[])
            asyncio.run(collect(client,dict(platform='wb',query='事件'),result,Path(tmp)/'result.json'))
        self.assertEqual(result['posts'][0]['text'],'原帖的完整长正文。')
        self.assertEqual(client.uri,'/statuses/extend')

    def test_xhs_get_signature_matches_wire_query(self):
        # Isolate the legacy module's global config from Flask's config module.
        code = '''
import asyncio
from urllib.parse import urlsplit
from discussion.crawler_worker import load_client
from xhshow import Xhshow
cls = load_client('xhs')
client = cls.__new__(cls)
class Signer:
    def sign_xs_get(self, uri, a1_value, params):
        client.signed = Xhshow()._build_content_string('GET', urlsplit(uri).path, params)
        assert params['cursor'] == '' and params['top_comment_id'] == ''
        return 'X' * 512
client._xhshow_client = Signer()
client.cookie_dict, client.headers, client.playwright_page = {}, {}, None
client._host = 'https://edith.xiaohongshu.com'
async def request(method, url, **kwargs):
    parsed = urlsplit(url)
    assert parsed.path + '?' + parsed.query == client.signed
    return {}
client.request = request
asyncio.run(client.get('/api/sns/web/v2/comment/page', {'note_id':'abc','cursor':'','top_comment_id':'','image_formats':'jpg,webp,avif','xsec_token':'token='}))
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_normalize_dedup_quota_and_no_credentials(self):
        value = sample()
        value["posts"] *= 9
        value["comments"] = [{"id": str(i), "parent_id": "post1", "text": "样本", "cookie": "SECRET"} for i in range(30)]
        value["posts"][0]["url"] = "javascript:alert(1)"
        evidence, comments = normalize(value, "xhs", "事件", "2026-11-17")
        self.assertEqual(len(evidence), 31)
        self.assertEqual(len(comments), 30)
        self.assertNotIn("SECRET", json.dumps(evidence + comments))
        self.assertTrue(all(e["url"].startswith("https://www.xiaohongshu.com/") for e in evidence))
        again, _ = normalize(value, "xhs", "不同关键词", "2026-11-18")
        self.assertEqual([e["id"] for e in evidence], [e["id"] for e in again])
        for bad in ["dy", [None], [{}], ["bad"]]:
            with self.assertRaises(ValueError):
                platforms(bad)

    def test_xhs_final_page_processed_without_post_cap(self):
        class Client:
            calls = 0
            async def get_note_by_keyword(self, query, **kwargs):
                return {"has_more": False, "items": [{"id": str(i)} for i in range(20)]}
            async def get_note_by_id(self, pid, source, token):
                self.calls += 1
                return {"note_id": pid, "desc": "正文"}
            async def get_note_comments(self, pid, token):
                return {"comments": [{"id": pid + "_" + str(i), "content": "评论"} for i in range(30)]}
        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            result = {"posts": [], "comments": [], "status": "running"}
            asyncio.run(collect(client, {"platform": "xhs", "query": "事件", "max_posts": 5, "max_comments": 10}, result, Path(tmp)/"result.json"))
            self.assertEqual(client.calls, 20)
            self.assertEqual(len(result["posts"]), 20)
            self.assertEqual(len(result["comments"]), 0)
            self.assertFalse(result["has_more"])

    def test_worker_timeout_preserves_partial_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = CrawlerBridge(tmp)
            class Process:
                def wait(self, timeout): raise subprocess.TimeoutExpired("worker", timeout)
            partial = dict(sample(), status="running", message="正在采集")
            with patch.object(bridge, '_spawn', return_value=Process()), patch.object(bridge, '_kill') as kill, patch.object(bridge, '_result', return_value=partial), patch('discussion.crawler.time.monotonic', side_effect=[0, 999]):
                record, evidence, comments = bridge.collect("meeting-123456789abc", 1, "dy", "事件")
                kill.assert_called_once()
                self.assertEqual(record["status"], "timeout")
                self.assertEqual(len(comments), 1)

    def test_active_page_can_run_longer_than_old_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge=CrawlerBridge(tmp)
            clock=[0]
            class Process:
                def __init__(self,folder): self.folder,self.n=folder,0
                def wait(self,timeout):
                    self.n+=1;clock[0]+=60
                    data=dict(posts=[],comments=[],status='running' if self.n<8 else 'complete',request_count=self.n,message='继续读取')
                    (self.folder/'result.json').write_text(json.dumps(data),encoding='utf-8')
                    os.utime(self.folder/'result.json',(clock[0],clock[0]))
                    if self.n<8: raise subprocess.TimeoutExpired('worker',timeout)
            def spawn(folder,*args):
                folder.mkdir(parents=True)
                self.assertNotIn('deadline',args[-1])
                return Process(folder)
            with patch.object(bridge,'_spawn',side_effect=spawn), patch.object(bridge,'_kill') as kill, patch('discussion.crawler.time.monotonic',side_effect=lambda:clock[0]):
                result,_,_=bridge.collect_page('meeting-123456789abc',1,dict(platform='xhs',query='事件'),90)
            self.assertEqual(result['status'],'complete')
            self.assertGreater(clock[0],300)
            kill.assert_not_called()


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(CrawlerFlowTests(name) for name in loader.getTestCaseNames(CrawlerFlowTests) if name.startswith('test_crawler_'))
    suite.addTests(loader.loadTestsFromTestCase(WorkerTests))
    return suite


if __name__ == '__main__':
    unittest.main(verbosity=2)
