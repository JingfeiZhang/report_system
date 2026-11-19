"""Deterministic integration tests: no paid calls, databases, or web services."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from discussion.api import register_discussion, render_report
from discussion import demo
from discussion.evidence import parse_upload, retrieve
from discussion.store import Conflict, Store
from discussion.providers import DiscussionWordBudget


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


class HoldingExecutor:
    def __init__(self): self.pending = []
    def submit(self, fn, *args): self.pending.append((fn, args))
    def run(self):
        fn, args = self.pending.pop(0)
        fn(*args)


class FakeProvider:
    def __init__(self): self.on_card = None
    def json(self, role, instruction, data):
        if '"covered"' in instruction:
            return dict(covered=False, action='stop', evidence_ids=[], reason='测试来源已结束', gaps=['原始材料'])
        if '"agenda"' in instruction:
            return {"agenda": ["核查故障时间线"], "roles": ["facts"]}
        if '"tasks"' in instruction:
            return {"tasks": [{"role": "facts", "question": "核查故障时间线", "feedback_ids": []}]}
        if '"overview"' in instruction:
            return {"overview": "完成证据核查", "agreements": [], "disagreements": [], "gaps": [], "next_questions": []}
        if '"labels"' in instruction:
            return {"labels": [{"id": c["id"], "label": "负面"} for c in data["comments"]]}
        if self.on_card:
            hook, self.on_card = self.on_card, None
            hook()
        return {"conclusion": "证据显示发生故障。", "evidence_ids": [e["id"] for e in data["evidence"][:1]],
                "limitations": ["仅用于测试"], "response": "已回应核查要求", "changes": "补充证据"}
    def search(self, query, time_range): return []
    def report(self, meeting, snapshot, folder, progress):
        return "# 测试报告\n故障时间线 [" + snapshot["evidence"][0]["id"] + "]"


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        self.app = Flask(__name__, root_path=str(root), template_folder="templates", static_folder="static")
        self.fake = FakeProvider()
        self.service = register_discussion(self.app, self.tmp.name, self.fake)
        self.service.pool.shutdown()
        self.service.pool = InlineExecutor()
        self.client = self.app.test_client()

    def tearDown(self): self.tmp.cleanup()

    def create(self, mode="demo"):
        response = self.client.post('/api/discussions', json={"title": "故障事件", "mode": mode, "web_enabled": False})
        self.assertEqual(response.status_code, 201)
        return response.json['id']

    def state(self, mid): return self.service.store.get(mid)
    def start(self, mid):
        return self.client.post(f'/api/discussions/{mid}/rounds', json={"expected_round": len(self.state(mid)['rounds'])})

    def test_demo_two_rounds_feedback_report_versions(self):
        mid = self.create()
        self.assertEqual(self.start(mid).status_code, 202)
        first = self.state(mid)
        self.assertEqual(first['status'], 'waiting')
        self.assertEqual(len(first['rounds']), 1)
        self.client.post(f'/api/discussions/{mid}/messages', json={"content": demo.FOLLOWUP, "target": "all"})
        self.start(mid)
        second = self.state(mid)
        self.assertEqual(second['messages'][0]['status'], 'answered')
        self.assertEqual(second['snapshots'][0], first['snapshots'][0])
        self.assertTrue(any('样本' in c['changes'] for c in second['rounds'][1]['cards']))
        for snapshot in second['snapshots']:
            response = self.client.post(f'/api/discussions/{mid}/reports', json={"snapshot_id": snapshot['id']})
            self.assertEqual(response.status_code, 202)
        final = self.state(mid)
        self.assertEqual([r['status'] for r in final['reports']], ['complete', 'complete'])
        rid = final['reports'][0]['id']
        self.assertEqual(self.client.get(f'/discussions/{mid}/reports/{rid}').status_code, 200)
        download = self.client.get(f'/discussions/{mid}/reports/{rid}?format=md')
        self.assertEqual(download.status_code, 200)
        download.close()
        self.assertEqual(self.client.post(f'/api/discussions/{mid}/reports', json={"snapshot_id": second['snapshots'][0]['id']}).status_code, 409)

    def test_freeform_demo_not_falsely_answered(self):
        mid = self.create()
        self.client.post(f'/api/discussions/{mid}/messages', json={"content": "调查火星上的投诉"})
        self.start(mid)
        self.assertEqual(self.state(mid)['messages'][0]['status'], 'recorded')

    def test_event_report_omits_operations_and_links_to_frozen_source(self):
        mid = self.create()
        self.start(mid)
        self.client.post(f'/api/discussions/{mid}/messages', json={'content': demo.FOLLOWUP})
        self.start(mid)
        snapshot = self.state(mid)['snapshots'][-1]
        self.client.post(f'/api/discussions/{mid}/reports', json={'snapshot_id': snapshot['id']})
        report = self.state(mid)['reports'][-1]
        self.assertEqual(report['status'], 'complete')
        url = f'/discussions/{mid}/reports/{report["id"]}'
        html = self.client.get(url).get_data(as_text=True)
        download = self.client.get(url+'?format=md')
        markdown = download.get_data(as_text=True)
        download.close()
        for heading in ('评论样本统计（程序计算）', '会议与资料范围', '采集范围与停止原因',
                        '研讨过程与版本记录', '用户指导处理记录', '可追溯证据', '证据目录'):
            self.assertNotIn(heading, html)
            self.assertNotIn(heading, markdown)
        self.assertNotIn(snapshot['id'], html)
        self.assertNotIn(demo.FOLLOWUP, markdown)
        cited = next(e for e in snapshot['evidence'] if e['id'] in markdown)
        source_url = url+'/sources/'+cited['id']
        self.assertIn(source_url, html)
        # Later evidence changes must not change the published citation target.
        self.service.store.change(mid, lambda s: s['evidence'][0].update(text='下一轮的新内容'))
        source = self.client.get(source_url)
        self.assertEqual(source.status_code, 200)
        self.assertIn(cited['text'], source.get_data(as_text=True))
        self.assertNotIn('下一轮的新内容', source.get_data(as_text=True))
        self.assertEqual(self.client.get(url+'/sources/E-000000000000').status_code, 404)
        other = self.create()
        self.assertEqual(self.client.get(f'/discussions/{other}/reports/{report["id"]}/sources/{cited["id"]}').status_code, 404)

    def test_upgraded_review_creates_new_report_without_overwriting_history(self):
        mid = self.create()
        self.start(mid)
        sid = self.state(mid)['snapshots'][0]['id']
        self.client.post(f'/api/discussions/{mid}/reports',json={'snapshot_id':sid})
        first = self.state(mid)['reports'][0]
        old_path = self.service.store.root/mid/first['id']/'report.md'
        old_bytes = old_path.read_bytes()
        self.service.store.change(mid, lambda s:s['reports'][0].update(review_version=1))
        response = self.client.post(f'/api/discussions/{mid}/reports',json={'snapshot_id':sid,'recheck':True})
        self.assertEqual(response.status_code,202)
        records = self.state(mid)['reports']
        self.assertEqual(len(records),2)
        self.assertEqual(records[1]['status'],'complete')
        self.assertEqual(records[1]['supersedes'],records[0]['id'])
        self.assertEqual(records[0]['superseded_by'],records[1]['id'])
        self.assertEqual(old_path.read_bytes(),old_bytes)
        self.assertEqual(self.client.post(f'/api/discussions/{mid}/reports',json={'snapshot_id':sid,'recheck':True}).status_code,409)

    def test_generation_upgrade_does_not_reuse_previous_engine_text(self):
        from unittest.mock import patch
        mid=self.create(); self.start(mid)
        sid=self.state(mid)['snapshots'][0]['id']
        self.client.post(f'/api/discussions/{mid}/reports',json={'snapshot_id':sid})
        old=self.state(mid)['reports'][0]
        self.service.store.change(mid,lambda s:s['reports'][0].update(generation_version=1))
        with patch('discussion.service.demo.report',wraps=demo.report) as generate:
            self.client.post(f'/api/discussions/{mid}/reports',json={'snapshot_id':sid,'recheck':True})
            self.assertEqual(generate.call_count,1)
        self.assertEqual(len(self.state(mid)['reports']),2)
        self.assertEqual(self.state(mid)['reports'][1]['supersedes'],old['id'])

    def test_duplicate_action_and_mid_round_input_frozen(self):
        mid = self.create('live')
        self.service.upload(mid, 'first.txt', '故障时间线：09:10开始，10:05恢复。'.encode())
        holder = HoldingExecutor()
        self.service.pool = holder
        self.start(mid)
        self.assertEqual(self.start(mid).status_code, 409)
        self.service.message(mid, {"content": "这条新指导应进入下一轮"})
        self.service.upload(mid, 'second.txt', '故障后续回应仅供下一轮使用。'.encode())
        holder.run()
        state = self.state(mid)
        self.assertEqual(state['status'], 'waiting')
        self.assertEqual(state['messages'][0]['status'], 'pending')
        self.assertEqual(len(state['snapshots'][0]['evidence']), 1)
        self.assertEqual(len(state['evidence']), 2)
        self.assertEqual(self.client.post(f'/api/discussions/{mid}/rounds', json={"expected_round": 0}).status_code, 409)

    def test_recovery_and_event_replay(self):
        mid = self.create()
        self.service.pool = HoldingExecutor()
        self.start(mid)
        other = Store(self.tmp.name)
        other.recover()
        self.assertEqual(other.get(mid)['status'], 'interrupted')
        rows = other.events(mid)
        self.assertTrue(rows)
        self.assertEqual(other.events(mid, rows[-1]['id']), [])
        response = self.client.get(f'/api/discussions/{mid}/events', headers={'Last-Event-ID': str(rows[-2]['id'])}, buffered=False)
        chunk = next(iter(response.response)).decode()
        self.assertIn('interrupted', chunk)
        response.close()

    def test_upload_retrieval_and_isolation(self):
        mid = self.create('live')
        other = self.create('live')
        self.service.upload(mid, '事实.txt', '故障时间线：登录接口09:10异常。\n\n退款规则不变。'.encode())
        self.assertEqual(self.state(other)['evidence'], [])
        results = retrieve('故障时间线 登录', self.state(mid)['evidence'])
        self.assertIn('09:10', results[0]['text'])
        self.service.upload(mid, 'worldbank.txt', 'Ukraine reconstruction and recovery needs total $524 billion as of December 31, 2024.'.encode())
        bilingual = retrieve('重建需求 Ukraine reconstruction recovery', self.state(mid)['evidence'])
        self.assertIn('$524', bilingual[0]['text'])
        with self.assertRaises(Conflict):
            self.service.upload(mid, '重复.txt', '故障时间线：登录接口09:10异常。\n\n退款规则不变。'.encode())
        self.assertEqual(self.client.post(f'/api/discussions/{mid}/materials', data={'file': (io.BytesIO(b'bad'), 'bad.exe')}).status_code, 400)

    def test_pdf_and_csv_parsing(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), 'Service failed at 09:10.')
        material, evidence, _ = parse_upload('report.pdf', doc.tobytes())
        doc.close()
        self.assertIn('09:10', evidence[0]['text'])
        self.assertIn('第 1 页', evidence[0]['locator'])
        _, _, comments = parse_upload('comments.csv', 'text,time\n太差了,2026-10-12\n太差了,2026-10-12\n'.encode())
        stats = self.service._sentiment(comments)
        self.assertEqual((stats['total'], stats['unique'], stats['counts']['负面']), (2, 1, 1))
        with self.assertRaises(ValueError): parse_upload('bad.csv', b'wrong\nvalue')
        with self.assertRaises(ValueError): parse_upload('bad.pdf', b'broken PDF')

    def test_feedback_original_reaches_role_and_is_preserved(self):
        mid = self.create('live')
        self.service.upload(mid, 'facts.txt', '故障时间线09:10发生异常。'.encode())
        self.start(mid)
        message = '请核查故障时间线，区分恢复与数据核对。'
        self.service.message(mid, {'content': message, 'target': 'facts'})
        seen = []
        original = self.fake.json
        def observe(role, instruction, data):
            if 'user_feedback' in data:
                seen.extend(m['content'] for m in data['user_feedback'])
            return original(role, instruction, data)
        self.fake.json = observe
        self.start(mid)
        self.assertIn(message, seen)
        self.start(mid)
        self.assertEqual(self.state(mid)['snapshots'][-1]['feedback'][0]['content'], message)

    def test_unknown_host_citation_prevents_snapshot(self):
        mid = self.create('live')
        self.service.upload(mid, 'facts.txt', '故障时间线09:10发生异常。'.encode())
        original = self.fake.json
        def corrupt(role, instruction, data):
            result = original(role, instruction, data)
            if 'overview' in result:
                result['overview'] = '未提供的证据 [E-000000000000]'
            return result
        self.fake.json = corrupt
        self.start(mid)
        self.assertEqual(self.state(mid)['status'], 'error')
        self.assertEqual(self.state(mid)['snapshots'], [])

    def test_report_budget_respects_short_report(self):
        from types import SimpleNamespace
        sections = [SimpleNamespace(chapter_id=f'S{i}', title=f'章节{i}') for i in range(3)]
        plan = DiscussionWordBudget(1200).run(sections)
        self.assertEqual(plan['totalWords'], 1200)
        self.assertEqual(sum(c['targetWords'] for c in plan['chapters']), 1200)
        self.assertEqual([c['chapterId'] for c in plan['chapters']], ['S0', 'S1', 'S2'])

    def test_report_only_uses_snapshot_citations(self):
        mid = self.create('live')
        self.service.upload(mid, 'facts.txt', '故障时间线09:10发生异常。'.encode())
        self.start(mid)
        self.fake.report = lambda *args: '# 不合法报告\n[E-000000000000]'
        self.client.post(f'/api/discussions/{mid}/reports', json={'snapshot_id': self.state(mid)['snapshots'][0]['id']})
        self.assertEqual(self.state(mid)['reports'][-1]['status'], 'failed')
        self.assertIn('引用', self.state(mid)['error'])

    def test_safe_render_and_live_failures(self):
        rendered = render_report('<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[x](javascript:alert(1))', [])
        cjk = render_report('**样本范围：**仅描述本次样本。', [])
        self.assertIn('<strong>样本范围：</strong>', cjk)
        self.assertNotIn('**', cjk)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('<img', rendered)
        self.assertNotIn('href=', rendered)
        table = render_report('| 内容 | 来源 |\n| --- | --- |\n| 故障 | [E-123456789abc] |', [{'id': 'E-123456789abc'}])
        self.assertIn('<table>', table)
        self.assertIn('href="#source-E-123456789abc"', table)
        adjacent = render_report('`[E-123456789abc]``[E-abcdef123456]`',
                                 [{'id':'E-123456789abc'}, {'id':'E-abcdef123456'}])
        self.assertEqual(adjacent.count('class="citation"'),2)
        self.assertNotIn('<code>',adjacent)
        superscript = render_report('x^2。^[E-123456789abc][E-abcdef123456]^。^[E-123456789abc]^',
                                   [{'id':'E-123456789abc'}, {'id':'E-abcdef123456'}])
        self.assertEqual(superscript.count('class="citation"'), 3)
        self.assertEqual(superscript.count('^'), 1)
        self.assertIn('x^2', superscript)
        mid = self.create('live')
        self.fake.json = lambda *args: (_ for _ in ()).throw(RuntimeError('模型不可用'))
        self.start(mid)
        state = self.state(mid)
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['snapshots'], [])
        self.assertEqual(state['mode'], 'live')

    def test_no_comments_no_sentiment_percentages(self):
        mid = self.create('live')
        self.service.update_agenda(mid, {'agenda': ['评论情感分析'], 'roles': ['sentiment']})
        original = self.fake.json
        proposals = []
        def only_sentiment(role, instruction, data):
            if '"tasks"' in instruction:
                return {'tasks': [{'role': 'sentiment', 'question': '分析评论', 'feedback_ids': []}]}
            if data.get('task', {}).get('phase') == 'proposal':
                proposals.append(data)
            return original(role, instruction, data)
        self.fake.json = only_sentiment
        self.start(mid)
        state = self.state(mid)
        self.assertIsNone(state['snapshots'][0]['sentiment'])
        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(d['sentiment'] is None and d['sample_scope']['comment_count'] == 0 for d in proposals))
        self.assertFalse(state['snapshots'][0]['evidence'])
        self.assertTrue(all(c['evidence_status'] != 'supported' for c in state['snapshots'][0]['claims']))


if __name__ == '__main__': unittest.main(verbosity=2)
