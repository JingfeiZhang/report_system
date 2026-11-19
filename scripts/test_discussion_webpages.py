import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from discussion.webpages import fetch_page, public_url, extract_article
from discussion.evidence import make_evidence


class Response:
    status_code=200
    is_redirect=False
    headers={'Content-Type':'text/html;charset=utf-8'}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_content(self,*args):
        yield ('<html><title>事件原文</title><nav>导航不得入库</nav><article>'+('通报正文，限定原文证据范围。'*30)+'</article></html>').encode()


class WebpageTests(unittest.TestCase):
    def test_short_div_notice_and_navigation(self):
        notice='服务通知：系统已于今日恢复，部分订单仍需重试。如需查询订单状态，请通过客服渠道联系工作人员。'
        text, method=extract_article('<div class="content_area">'+notice+'</div><nav>其他新闻推荐</nav>')
        self.assertEqual(text,notice)
        self.assertEqual(method,'article_container')

    def test_generic_prose_fallback_excludes_menu_and_single_long_paragraph(self):
        prose='这是一则事件报道，介绍了事件经过和当事人的回应。'*7
        text,_=extract_article('<body><div><a href="/">导航链接</a></div><div>'+prose+'</div></body>')
        self.assertEqual(text,prose)
        self.assertEqual(extract_article('<body><p>'+prose+'</p></body>')[0],prose)

    def test_link_directory_and_login_page_are_not_articles(self):
        links=''.join('<a href="/">这是新闻标题及其内容简介。</a>' for _ in range(30))
        self.assertEqual(extract_article('<article>'+links+'</article>')[0],'')
        self.assertEqual(extract_article('<title>安全验证</title><article>'+('正文。'*60)+'</article>')[1],'access_page')

    def test_empty_js_article_uses_rendered_text(self):
        response=Response()
        response.iter_content=lambda *args:iter([b'<html><div id="content_area"></div><script src="/article.js"></script></html>'])
        text='浏览器加载后的文章正文。'*15
        with patch('discussion.webpages.public_url'), patch('requests.Session.get',return_value=response), patch('discussion.webpages.render_article',return_value=(text,'browser_article_container',self.source['url'])) as render:
            pages,record,body=fetch_page(self.source)
        render.assert_called_once()
        self.assertEqual(record['status'],'fetched')
        self.assertEqual(body,text)
        self.assertEqual(pages[0]['extraction'],'browser_article_container')

    def test_browser_failure_preserves_summary_not_fake_body(self):
        response=Response()
        response.iter_content=lambda *args:iter([b'<div id="content_area"></div><script></script>'])
        with patch('discussion.webpages.public_url'), patch('requests.Session.get',return_value=response), patch('discussion.webpages.render_article',side_effect=TimeoutError):
            pages,record,body=fetch_page(self.source)
        self.assertEqual(pages,[])
        self.assertEqual(body,'')
        self.assertEqual(record['status'],'render_failed')

    def setUp(self):
        self.source=make_evidence('摘要','事件来源','摘要','web_summary','https://example.com/article')

    def test_extract_retains_provenance_and_excludes_navigation(self):
        with patch('discussion.webpages.public_url'), patch('requests.Session.get',return_value=Response()):
            pages,record,text=fetch_page(self.source)
        self.assertEqual(record['status'],'fetched')
        self.assertNotIn('导航不得入库',text)
        self.assertEqual(pages[0]['source_type'],'web_page')
        self.assertEqual(pages[0]['parent_summary_id'],self.source['id'])

    def test_http_failure_never_fabricates_fulltext(self):
        response=Response();response.status_code=404
        with patch('discussion.webpages.public_url'), patch('requests.Session.get',return_value=response):
            pages,record,text=fetch_page(self.source)
        self.assertEqual(pages,[])
        self.assertEqual(record['http_status'],404)
        self.assertEqual(record['status'],'http_error')

    def test_private_targets_and_redirects_blocked(self):
        with self.assertRaises(ValueError):public_url('http://127.0.0.1/')
        response=Response();response.is_redirect=True;response.headers={'Location':'http://127.0.0.1/'}
        with patch('discussion.webpages.public_url',side_effect=[None,ValueError('private')]), patch('requests.Session.get',return_value=response) as request:
            pages,record,text=fetch_page(self.source)
        self.assertEqual(request.call_count,1)
        self.assertEqual(record['status'],'fetch_failed')

    def test_stop_does_not_start_request(self):
        with patch('requests.Session.get') as request:
            pages,record,text=fetch_page(self.source,stopped=lambda:True)
        request.assert_not_called()
        self.assertEqual(record['status'],'stopped')


if __name__=='__main__':unittest.main(verbosity=2)
