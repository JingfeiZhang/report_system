"""Image provenance, safe file handling and no-model backfill regression tests."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from flask import Flask
from discussion.api import register_discussion
from discussion.images import post_images, image_url, ImageCollector, ImageDownloadError, attachment_path
from discussion.crawler_worker import write_post
from discussion.crawler import normalize
from test_discussion import FakeProvider, InlineExecutor, HoldingExecutor


def image_bytes():
    stream=io.BytesIO()
    Image.new('RGB',(120,80),'#24758a').save(stream,format='PNG')
    return stream.getvalue()


class ImagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.mid='meeting-aaaaaaaaaaaa'
    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_images_and_quote_provenance_without_avatars(self):
        item=dict(id='101',pics=[dict(pid='p1',url='https://wx1.sinaimg.cn/small/a.jpg',large=dict(url='https://wx1.sinaimg.cn/large/a.jpg'))],
                  retweeted_status=dict(id='202',pics=[dict(pid='p2',url='https://wx2.sinaimg.cn/large/b.jpg')]),
                  user=dict(avatar='https://wx1.sinaimg.cn/avatar.jpg'))
        images=post_images(item,'wb')
        self.assertEqual(len(images),2)
        self.assertEqual(images[0]['urls'][0],'https://wx1.sinaimg.cn/large/a.jpg')
        self.assertEqual(images[1]['origin_post_id'],'202')
        self.assertEqual(images[1]['kind'],'转发所附原帖配图')
        self.assertFalse(post_images(item,'dy'))
        result=dict(posts=[])
        write_post(result,dict(note_id='imgonly',image_list=[dict(url_default='https://sns-webpic-qc.xhscdn.com/image.jpg')]),'xhs','查询')
        self.assertEqual(len(result['posts']),1)
        self.assertIn('尚未识别',result['posts'][0]['text'])

    def test_file_cache_duplicate_hash_and_original(self):
        c=ImageCollector(self.root,self.mid)
        choices=post_images(dict(id='101',pics=[dict(url=f'https://wx1.sinaimg.cn/{n}.jpg') for n in range(7)]),'wb')
        with patch.object(c,'_download',return_value=(image_bytes(),choices[0]['urls'][0])) as download:
            post=dict(image_candidates=choices)
            c.collect_posts([post],'wb')
            self.assertEqual(len(post['images']),7)
            self.assertEqual(download.call_count,7)
            again=c.fetch(choices[0],'wb')
            self.assertEqual(download.call_count,7)
        self.assertEqual(len({i['sha256'] for i in post['images']}),1)
        self.assertEqual(attachment_path(self.root,self.mid,again['sha256'],True).read_bytes(),image_bytes())
        self.assertEqual(again['analysis_status'],'not_analyzed')
        with self.assertRaises(KeyError):attachment_path(self.root,self.mid,'../secret')

    def test_reject_urls_and_non_images(self):
        for url in ('file:///a','http://127.0.0.1/a','https://wx1.sinaimg.cn.evil.com/a','https://user:pass@wx1.sinaimg.cn/a','https://wx1.sinaimg.cn:9000/a'):
            self.assertEqual(image_url(url,'wb'),'')
        c=ImageCollector(self.root,self.mid)
        candidate=post_images(dict(id='1',pics=[dict(url='https://wx1.sinaimg.cn/a')]),'wb')[0]
        with patch.object(c,'_download',return_value=(b'<svg onload="alert(1)"></svg>',candidate['urls'][0])):
            record=c.fetch(candidate,'wb')
        self.assertEqual(record['error_code'],'invalid_image')
        with patch('discussion.images.public_url',side_effect=ValueError('non_public_address')):
            with self.assertRaisesRegex(ImageDownloadError,'blocked_url'):c._download(candidate['urls'][0],'wb')

    def test_stop_makes_no_requests_and_does_not_claim_analysis(self):
        c=ImageCollector(self.root,self.mid,lambda:True)
        candidate=post_images(dict(id='1',pics=[dict(url='https://wx1.sinaimg.cn/a')]),'wb')[0]
        with patch.object(c,'_download') as download:
            posts=c.collect_posts([dict(image_candidates=[candidate])],'wb')
        download.assert_not_called()
        self.assertEqual(posts[0]['images'][0]['status'],'stopped')
        self.assertEqual(posts[0]['images'][0]['analysis_status'],'not_analyzed')

    def test_backfill_no_model_freezes_history_and_serves_scoped_files(self):
        app=Flask(__name__,root_path=str(Path(__file__).resolve().parents[1]))
        provider=FakeProvider()
        service=register_discussion(app,self.root,provider)
        service.pool.shutdown();service.pool=InlineExecutor()
        client=app.test_client()
        mid=client.post('/api/discussions',json=dict(title='测试',mode='live',web_enabled=False)).json['id']
        raw=dict(id='123',text='配图说明',pics=[dict(url='https://wx1.sinaimg.cn/a.jpg')])
        result=dict(posts=[])
        write_post(result,raw,'wb','测试')
        evidence,_=normalize(result,'wb','测试','2026-11-18')
        def setup(s):
            s['evidence']=evidence
            s['snapshots']=[dict(id='snapshot-test',round=1,created_at='2026-11-18',summary={},evidence=evidence)]
        service.store.change(mid,setup)
        old=service.store.get(mid)['snapshots']
        folder=self.root/mid/'crawls'/'page'
        folder.mkdir(parents=True)
        (folder/'job.json').write_text(json.dumps(dict(platform='wb')),encoding='utf-8')
        (folder/'response-1.json').write_text(json.dumps(dict(data=[dict(mblog=raw)])),encoding='utf-8')
        provider.json=Mock(side_effect=AssertionError('No model allowed'))
        provider.search=Mock(side_effect=AssertionError('No search allowed'))
        with patch.object(ImageCollector,'_download',return_value=(image_bytes(),'https://wx1.sinaimg.cn/a.jpg')):
            self.assertEqual(client.post(f'/api/discussions/{mid}/images/collect').status_code,202)
        current=service.store.get(mid)
        self.assertEqual(current['snapshots'],old)
        self.assertEqual(current['image_collection']['downloaded'],1)
        self.assertEqual(current['status'],'ready')
        provider.json.assert_not_called();provider.search.assert_not_called()
        record=current['evidence'][0]['images'][0]
        self.assertEqual(client.get(f'/api/discussions/{mid}/evidence?source=image').json['total'],1)
        url=f"/api/discussions/{mid}/images/{record['sha256']}"
        response=client.get(url)
        self.assertEqual(response.mimetype,'image/jpeg');response.close()
        response=client.get(url+'?download=1')
        self.assertEqual(response.data,image_bytes())
        self.assertIn('attachment',response.headers['Content-Disposition']);response.close()
        other=service.store.get(mid);other.update(id='meeting-bbbbbbbbbbbb',evidence=[],snapshots=[])
        service.store.create(other)
        self.assertEqual(client.get(url.replace(mid,other['id'])).status_code,404)
        service.pool=HoldingExecutor()
        client.post(f'/api/discussions/{mid}/images/collect')
        self.assertEqual(client.post(f'/api/discussions/{mid}/images/collect').status_code,409)
        self.assertEqual(client.post(f'/api/discussions/{mid}/rounds',json={}).status_code,409)
        client.post(f'/api/discussions/{mid}/images/stop')
        service.pool.run()
        self.assertEqual(service.store.get(mid)['image_collection']['status'],'stopped')


if __name__=='__main__':unittest.main()
