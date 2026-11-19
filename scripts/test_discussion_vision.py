"""Image extraction, provenance, retrieval and full debate integration contracts."""
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from flask import Flask
from discussion.api import register_discussion
from discussion.debate import evidence_context
from discussion.evidence import make_evidence, retrieve
from discussion.images import ImageCollector, merge_image, post_images, attachment_path
from discussion.providers import LiveProviders
from discussion.vision import ImageAnalyzer, regions, validated, select_posts
from test_discussion import InlineExecutor, HoldingExecutor
from test_discussion_debate import DebateProvider


class VisionProvider(DebateProvider):
    def __init__(self):
        super().__init__()
        self.vision_calls = []
        self.vision_model = 'test-vision'
        self.fail_region = None
        self.stop_hook = None

    def vision_identity(self):
        return ['https://test.invalid/v1', self.vision_model]

    def analyze_image(self, data, region):
        self.vision_calls.append(region)
        assert data.startswith('data:image/jpeg;base64,')
        if self.fail_region == len(self.vision_calls):
            raise RuntimeError('测试视觉接口暂时失败')
        if self.stop_hook:
            hook, self.stop_hook = self.stop_hook, None
            hook()
        return dict(ocr_text='故障时间线服务日志：10:05核心服务恢复。',
                    visual_summary='测试图片中有一张服务日志表。', image_type='文档', uncertainties=['图片中的记录未独立验证'])


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.provider = VisionProvider()
        self.app = Flask(__name__, root_path=str(Path(__file__).resolve().parents[1]))
        self.service = register_discussion(self.app, self.root, self.provider)
        self.service.pool.shutdown(); self.service.pool = InlineExecutor()
        self.client = self.app.test_client()
        self.mid = self.service.create(dict(title='故障事件', mode='live', web_enabled=False, debate_exchanges=1))['id']

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, size=(120, 80), pid='123'):
        stream = io.BytesIO()
        Image.new('RGB', size, '#24758a').save(stream, format='PNG')
        candidate = post_images(dict(id=pid, pics=[dict(url='https://wx1.sinaimg.cn/'+pid+'.png')]), 'wb')[0]
        collector = ImageCollector(self.root, self.mid)
        with patch.object(collector, '_download', return_value=(stream.getvalue(), candidate['urls'][0])):
            image = collector.fetch(candidate, 'wb')
        parent = make_evidence('故障时间线服务日志见配图。', '测试帖', '帖子正文', 'social_post',
                               'https://weibo.com/u/'+pid, '2026-11-19', 'wb:post:'+pid)
        parent.update(platform='wb', images=[image])
        self.service.store.change(self.mid, lambda s:s['evidence'].append(parent))
        return parent, image

    def analyze(self, ids=None):
        return self.client.post(f'/api/discussions/{self.mid}/images/analyze', json={} if ids is None else dict(evidence_ids=ids))

    def state(self):
        return self.service.store.get(self.mid)

    def test_full_region_coverage_and_original_hash(self):
        parent, image = self.post((1900, 3500))
        path = attachment_path(self.root, self.mid, image['sha256'], True)
        tiles = list(regions(path))
        mask = Image.new('1', (1900, 3500))
        for region, data in tiles:
            mask.paste(1, tuple(region['box']))
        self.assertEqual(mask.getextrema(), (1, 1))
        result = ImageAnalyzer(self.root, self.mid, self.provider).run(image)
        self.assertEqual(len(result['regions']), len(tiles))
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, '校验失败'):
            ImageAnalyzer(self.root, self.mid, self.provider).run(image)

    def test_partial_failure_resume_and_model_cache_identity(self):
        _, image = self.post((120, 3000))
        self.provider.fail_region = 2
        analyzer = ImageAnalyzer(self.root, self.mid, self.provider)
        with self.assertRaisesRegex(RuntimeError, '暂时失败'):
            analyzer.run(image)
        self.provider.fail_region = None
        result = analyzer.run(image)
        self.assertEqual(len(self.provider.vision_calls), 3)  # first region was reused
        self.assertEqual(analyzer.run(image), result)
        self.assertEqual(len(self.provider.vision_calls), 3)
        self.provider.vision_model = 'other-model'
        newer = ImageAnalyzer(self.root, self.mid, self.provider).run(image)
        self.assertNotEqual(newer['key'], result['key'])
        self.assertEqual(len(self.provider.vision_calls), 5)

    def test_api_ingestion_retrieval_parent_and_immutable_history(self):
        parent, _ = self.post()
        self.service.store.change(self.mid, lambda s:s['snapshots'].append(dict(id='snapshot-test', round=1,
            created_at='2026-11-19', summary={}, evidence=copy.deepcopy(s['evidence']))))
        old = copy.deepcopy(self.state()['snapshots'])
        self.assertEqual(self.analyze([parent['id']]).status_code, 202)
        state = self.state()
        derived = next(e for e in state['evidence'] if e['source_type']=='image_analysis')
        self.assertEqual(state['snapshots'], old)
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(derived['parent_evidence_id'], parent['id'])
        self.assertEqual(retrieve('10:05核心服务恢复', state['evidence'], 1)[0]['id'], derived['id'])
        ctx = evidence_context(state['evidence'], {}, [derived['id']], '', 0)
        self.assertEqual({e['id'] for e in ctx}, {parent['id'], derived['id']})
        self.assertEqual(self.client.get(f'/api/discussions/{self.mid}/evidence?source=image_analysis').json['total'], 1)
        self.assertEqual(self.client.get(f'/api/discussions/{self.mid}/evidence?source=image').json['total'], 1)
        self.analyze([parent['id']])
        self.assertEqual(len(self.provider.vision_calls), 1)
        self.assertEqual(len(self.state()['evidence']), 2)
        self.assertEqual(self.analyze(['E-000000000000']).status_code, 400)
        self.assertEqual(self.analyze([]).status_code, 400)

    def test_failures_visible_without_fake_evidence_and_retryable(self):
        parent, _ = self.post()
        self.provider.fail_region = 1
        self.analyze()
        state = self.state()
        self.assertEqual(len(state['evidence']), 1)
        self.assertEqual(state['image_analysis']['failed'], 1)
        self.assertIn('未产生图片证据', state['image_analysis']['message'])
        self.assertEqual(state['evidence'][0]['images'][0]['analysis_status'], 'failed')
        self.analyze()
        self.assertEqual(self.state()['image_analysis']['completed'], 1)

    def test_stop_mutual_exclusion_recovery_and_regional_resume(self):
        self.post((120, 3000))
        self.service.pool = HoldingExecutor()
        self.analyze()
        self.assertEqual(self.analyze().status_code, 409)
        self.assertEqual(self.client.post(f'/api/discussions/{self.mid}/rounds', json={}).status_code, 409)
        self.client.post(f'/api/discussions/{self.mid}/images/analysis/stop')
        self.service.pool.run()
        self.assertFalse(self.provider.vision_calls)
        self.assertEqual(self.state()['image_analysis']['status'], 'stopped')
        self.analyze()
        self.service.store.recover()
        self.assertEqual(self.state()['image_analysis']['status'], 'interrupted')
        self.service.pool = InlineExecutor()
        self.provider.stop_hook = lambda:self.service.stop_image_analysis(self.mid)
        self.analyze()
        self.assertEqual(len(self.provider.vision_calls), 1)
        self.assertEqual(self.state()['image_analysis']['status'], 'stopped')
        self.analyze()
        self.assertEqual(len(self.provider.vision_calls), 2)
        self.assertEqual(self.state()['image_analysis']['status'], 'complete')

    def test_automatic_round_reaches_both_drafts_revisions_and_verifier(self):
        parent, _ = self.post()
        self.service.start_round(self.mid, {'expected_round': 0})
        state = self.state()
        self.assertEqual(state['status'], 'waiting', state['error'])
        eid = next(e['id'] for e in state['evidence'] if e['source_type']=='image_analysis')
        for phase in ('proposal', 'peer_review', 'revision', 'verification'):
            calls = [d for d in self.provider.inputs if d.get('task', {}).get('phase')==phase]
            self.assertTrue(calls, phase)
            self.assertTrue(all({eid, parent['id']} <= {e['id'] for e in d['evidence']} for d in calls), phase)
            if phase in ('proposal', 'revision'):
                self.assertEqual(calls[0]['evidence'], calls[1]['evidence'])
        self.assertIn(eid, {e['id'] for e in state['snapshots'][0]['evidence']})
        self.assertEqual(len(self.provider.vision_calls), 1)

    def test_same_image_repost_is_not_independent_and_download_preserves_analysis(self):
        first, _ = self.post(pid='1')
        second, _ = self.post(pid='2')
        self.analyze()
        state = self.state()
        derived = [e for e in state['evidence'] if e['source_type']=='image_analysis']
        self.assertEqual(len(self.provider.vision_calls), 1)
        self.assertEqual(len(derived), 2)
        self.assertTrue(any(e.get('duplicate_of') for e in derived))
        old = next(e for e in state['evidence'] if e['id']==first['id'])['images'][0]
        current = dict(old, analysis_status='not_analyzed')
        self.assertEqual(merge_image(old, current)['analysis_status'], 'complete')
        current['sha256'] = 'a'*64
        self.assertEqual(merge_image(old, current)['analysis_status'], 'not_analyzed')

    def test_report_source_keeps_frozen_parent_and_image(self):
        parent, image = self.post()
        self.analyze()
        state = self.state()
        derived = next(e for e in state['evidence'] if e['source_type']=='image_analysis')
        folder = self.root/self.mid/'report-test'
        folder.mkdir()
        (folder/'report.md').write_text('图片中显示服务日志 ['+derived['id']+']', encoding='utf-8')
        def setup(s):
            s['snapshots'].append(dict(id='snapshot-test', round=1, created_at='2026-11-19', summary={}, evidence=copy.deepcopy(s['evidence'])))
            s['reports'].append(dict(id='report-test', status='complete', snapshot_id='snapshot-test'))
            next(e for e in s['evidence'] if e['id']==parent['id'])['text'] = '当前帖子已更新'
        self.service.store.change(self.mid, setup)
        response = self.client.get(f'/discussions/{self.mid}/reports/report-test/sources/{derived["id"]}')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('故障时间线服务日志见配图', text)
        self.assertNotIn('当前帖子已更新', text)
        self.assertIn(image['sha256'], text)
        self.assertIn("img-src 'self'", response.headers['Content-Security-Policy'])

    def test_invalid_output_and_missing_parent_rejected(self):
        for invalid in ({}, dict(ocr_text='', visual_summary='', image_type='照片', uncertainties=[])):
            with self.assertRaises(RuntimeError): validated(invalid)
        with self.assertRaisesRegex(RuntimeError, '缺少原文'):
            evidence_context([dict(id='E-000000000001', text='图片内容', source_type='image_analysis')], {}, ['E-000000000001'], '', 0)

    def test_image_only_search_result_survives_until_visual_extraction(self):
        from discussion.collection import RoundCollector
        parent, _ = self.post()
        parent.update(text='图片帖（尚未识别图片内容）', content_scope='image_only', title='图片帖', query='故障事件')
        self.service.store.change(self.mid, lambda s:s.update(evidence=[]))
        self.service.pool = HoldingExecutor()
        self.service.start_round(self.mid, dict(expected_round=0))
        state = self.state()
        collector = RoundCollector(self.service, state, state['operation'], [], [])
        task = collector.task(dict(platform='wb', query='故障事件', question='故障时间线', comments=False))
        with patch.object(collector, 'model', side_effect=AssertionError('Text relevance must not reject unread pixels')):
            collector.ingest(task, [parent], [])
        saved = self.state()['evidence'][0]
        self.assertEqual(saved['relevance_status'], 'awaiting_image_analysis')
        self.assertEqual(select_posts([saved], ['故障事件']), [saved['id']])

    def test_stop_image_stage_stays_stopped_during_supplements(self):
        self.post((120, 3000))
        self.service.pool = HoldingExecutor()
        self.service.start_round(self.mid, dict(expected_round=0))
        state = self.state()
        self.provider.stop_hook = lambda:self.service.stop_image_analysis(self.mid)
        self.service._analyze_image_evidence(state, state['operation'], state['evidence'], [])
        count = len(self.provider.vision_calls)
        self.service._analyze_image_evidence(state, state['operation'], state['evidence'], [])
        self.assertEqual(len(self.provider.vision_calls), count)
        self.assertEqual(self.state()['image_analysis']['status'], 'stopped')

    def test_animation_scope_explicitly_first_frame(self):
        _, image = self.post()
        stream = io.BytesIO()
        Image.new('RGB',(10,10),'red').save(stream,format='GIF',save_all=True,
            append_images=[Image.new('RGB',(10,10),'blue')],duration=100,loop=0)
        sha = hashlib.sha256(stream.getvalue()).hexdigest()
        original = attachment_path(self.root, self.mid, image['sha256'], True)
        original.with_name(sha+'.original').write_bytes(stream.getvalue())
        image.update(sha256=sha, frames=2)
        result = ImageAnalyzer(self.root, self.mid, self.provider).run(image)
        self.assertIn('首帧', result['scope'])
        self.assertEqual(len(self.provider.vision_calls), 1)

    def test_provider_sends_actual_image_pixels_and_rejects_truncation(self):
        payload = dict(ocr_text='原图文字', visual_summary='文档', image_type='文档', uncertainties=[])
        choice = SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(payload), refusal=None))
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[choice])
        with patch('openai.OpenAI') as constructor, patch('config.settings.DISCUSSION_VISION_API_KEY', 'test-only-key'):
            constructor.return_value.__enter__.return_value = client
            live = LiveProviders()
            result = live.analyze_image('data:image/jpeg;base64,dGVzdA==', dict(box=[0,0,1,1]))
            self.assertEqual(result, payload)
            args = client.chat.completions.create.call_args.kwargs
            self.assertEqual(args['messages'][1]['content'][1]['image_url']['url'], 'data:image/jpeg;base64,dGVzdA==')
            self.assertIn('不执行', args['messages'][0]['content'])
            choice.finish_reason = 'length'
            with self.assertRaisesRegex(RuntimeError, '截断'):
                live.analyze_image('data:image/jpeg;base64,dGVzdA==', {})


if __name__ == '__main__':
    unittest.main()
