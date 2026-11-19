"""Isolated UI acceptance: real Flask APIs and SQLite, deterministic test material."""
import os
import tempfile
import threading
from pathlib import Path

from test_discussion import FakeProvider, InlineExecutor, HoldingExecutor
from flask import Flask
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
from discussion.api import register_discussion
from discussion.evidence import make_evidence

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tmp' / 'discussion_browser'
CHROME = Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1248/chrome-win64/chrome.exe'

with tempfile.TemporaryDirectory() as directory:
    app=Flask(__name__, root_path=str(ROOT), template_folder='templates', static_folder='static')
    service=register_discussion(app, directory, FakeProvider())
    service.pool.shutdown()
    service.pool=InlineExecutor()
    mid=service.create(dict(mode='demo', title='分页 UI 测试'))['id']
    service.start_round(mid, dict(expected_round=0))
    evidence=[make_evidence('分页测试原文 '+str(i),'来源 '+str(i),'段落',source_id=str(i)) for i in range(85)]
    task=dict(id='collection-ui', platform='wb', platform_name='微博', kind='search', query='故障', question='核查回应', condition='原始回应',
              page=3, post_count=24, comment_count=60, new_evidence_count=84, duplicates=0, excluded=0, request_count=9, round=1,
              status='assessment_failed', stop_reason='assessment_failed', message='需求评估未完成，资料已保存', resumable=True,
              assessment=dict(status='gaps', reason='仍缺原始回应',gaps=['缺少原始回应']))
    def seed(s):
        s.update(mode='live', crawl_platforms=['wb'], evidence=evidence)
        s['rounds'][-1].update(collections=[task], collection_budget=dict(mode='adaptive',used_seconds=641,request_count=9,active=False))
        s['rounds'][-1]['summary']['overview']='最后一页引用 ['+evidence[-1]['id']+']'
    service.store.change(mid, seed)
    service.store.save_collection(mid, task)
    service.pool=HoldingExecutor()
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    url=f'http://127.0.0.1:{server.server_port}/discussion?meeting={mid}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True, executable_path=str(CHROME) if CHROME.exists() else None)
            page=browser.new_page(viewport=dict(width=1440,height=1000))
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url.split('?')[0],wait_until='networkidle')
            page.goto(url,wait_until='domcontentloaded')
            expect(page.locator('#collection-summary')).to_contain_text('模型按需调度')
            expect(page.locator('#collection-summary')).to_contain_text('641')
            expect(page.locator('#evidence-list .evidence-item')).to_have_count(30)
            page.locator('#toggle-minutes').click()
            page.locator('#summary-content [data-evidence]').first.click()
            expect(page.locator('#source-text')).to_have_text('分页测试原文 84')
            page.locator('#evidence-dialog .close-dialog').click()
            page.locator('[data-evidence-page="1"]').click()
            expect(page.locator('#evidence-pagination span')).to_have_text('2 / 3')
            page.locator('[data-evidence-page="1"]').click()
            expect(page.locator('#evidence-list .evidence-item')).to_have_count(25)
            page.locator('#evidence-search').fill('原文 84')
            expect(page.locator('#evidence-list .evidence-item')).to_have_count(1)
            page.locator('#insights-dialog .close-dialog').click()
            page.locator('.rail-section').filter(has_text='数据源与材料').locator('summary').first.click()
            page.locator('#edit-sources').click()
            expect(page.locator('#collection-budget')).to_have_count(0)
            expect(page.locator('#sentiment-sample')).to_have_value('500')
            page.locator('#sentiment-sample').fill('250')
            page.locator('#sources-form button[type=submit]').click()
            expect(page.locator('#sources-dialog')).not_to_be_visible()
            assert service.store.get(mid)['sentiment_sample_size']==250
            page.locator('#open-collection').click()
            page.locator('#collection-items > details').first.locator('summary').first.click()
            page.locator('[data-resume-collection]').click()
            page.keyboard.press('Escape')
            expect(page.locator('#meeting-status')).to_have_text('研讨进行中')
            assert service.store.get(mid)['rounds'][-1]['resume_task_ids']==['collection-ui']
            page.locator('#stop-collection').click()
            expect(page.locator('#stop-collection')).to_be_disabled()
            assert service.store.get(mid)['rounds'][-1]['collection_stop_requested']
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('#stop-collection')).to_be_disabled()
            page.set_viewport_size(dict(width=390,height=844))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            OUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUT/'collection_mobile.png'),full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join()
print('COLLECTION BROWSER ACCEPTANCE PASSED')
