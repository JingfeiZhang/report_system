"""Export one published platform report unchanged to HTML, Markdown and PDF."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('meeting')
    parser.add_argument('report')
    parser.add_argument('output', type=Path)
    parser.add_argument('--base', default='http://127.0.0.1:5000')
    args = parser.parse_args()
    state = requests.get(f'{args.base}/api/discussions/{args.meeting}', timeout=30).json()
    report = next(r for r in state['reports'] if r['id'] == args.report)
    if report['status'] != 'complete':
        raise RuntimeError('Only published reports can be exported')
    url = f'{args.base}/discussions/{args.meeting}/reports/{args.report}'
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    markdown = requests.get(url+'?format=md', timeout=30)
    markdown.raise_for_status()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'report.md').write_bytes(markdown.content)
    html = response.content.decode('utf-8')
    css = requests.get(args.base+'/static/discussion/report.css', timeout=30)
    css.raise_for_status()
    js = requests.get(args.base+'/static/discussion/report.js', timeout=30)
    js.raise_for_status()
    html = html.replace('<link rel="stylesheet" href="/static/discussion/report.css">',
                        '<style>'+css.content.decode('utf-8')+'</style>')
    html = html.replace('<script defer src="/static/discussion/report.js"></script>', '')
    html = html.replace('</body>', '<script>'+js.content.decode('utf-8')+'</script></body>')
    html = html.replace('href="?format=md"','href="report.md"')
    html = html.replace('href="/discussion?', 'href="'+args.base+'/discussion?')
    html = html.replace('href="/discussions/', 'href="'+args.base+'/discussions/')
    (args.output/'report.html').write_text(html, encoding='utf-8')
    chrome = os.environ.get('DISCUSSION_TEST_CHROME', str(Path(os.environ['LOCALAPPDATA']) /
                         'ms-playwright/chromium-1248/chrome-win64/chrome.exe'))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=chrome)
        page = browser.new_page(viewport={'width':1280,'height':900})
        page.goto(url, wait_until='networkidle')
        page.evaluate('document.fonts.ready')
        # PDF is the published HTML print view; no second text generation.
        page.pdf(path=str(args.output/'report.pdf'), format='A4', print_background=True,
                 prefer_css_page_size=True, display_header_footer=True,
                 header_template='<span></span>',
                 footer_template='<div style="font-size:9px;text-align:center;width:100%;color:#777"><span class="pageNumber"></span> / <span class="totalPages"></span></div>')
        page.screenshot(path=str(args.output/'report-preview.png'))
        cited = page.locator('article .citation').count()
        assert cited > 0
        article_text = page.locator('article').inner_text()
        assert article_text.strip()
        assert not re.search(r'E-[a-f0-9]{12}', article_text), 'Raw evidence IDs remain visible in report body'
        links = page.locator('article .citation').evaluate_all('(nodes) => [...new Set(nodes.map(a => a.getAttribute("href")))]')
        for link in links:
            if link.startswith('#'):
                assert page.locator(link).count()
            else:
                assert link.startswith(f'/discussions/{args.meeting}/reports/{args.report}/sources/')
                assert page.request.get(args.base+link).status == 200
        browser.close()
    body = markdown.content.decode('utf-8').split('## 评论样本统计（程序计算）')[0]
    manifest = dict(meeting=args.meeting,report=args.report,snapshot=report['snapshot_id'],
                    url=url,body_chinese_characters=len(re.findall(r'[\u4e00-\u9fff]',body)),
                    citation_links=cited,files={})
    for name in ('report.md','report.html','report.pdf'):
        content = (args.output/name).read_bytes()
        manifest['files'][name] = dict(bytes=len(content),sha256=hashlib.sha256(content).hexdigest())
    (args.output/'export-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
