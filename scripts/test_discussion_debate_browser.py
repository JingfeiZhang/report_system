"""Browser checks for debate cards, targeted guidance, source pages and safe rendering."""
import json
import os
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tmp' / 'discussion_browser'
URL = json.loads((OUT / 'result.json').read_text(encoding='utf-8'))['meeting_url']
CHROME = Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1248/chrome-win64/chrome.exe'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=str(CHROME) if CHROME.exists() else None)
    page = browser.new_page(viewport={'width': 1600, 'height': 1000})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(URL.split('?')[0])
    page.wait_for_load_state('networkidle')
    # A meeting opens an intentionally persistent SSE request; wait for UI readiness.
    page.goto(URL, wait_until='domcontentloaded')
    page.locator('.debate-issue').first.wait_for()
    assert page.locator('.debate-issue').first.locator('.answer-message.left').count() >= 1
    assert page.locator('.debate-issue').first.locator('.answer-message.right').count() >= 1
    assert page.locator('.challenge-item').count() >= 2
    page.locator('.claim-details summary').first.click()
    page.locator('[data-ask-claim]').first.click()
    assert '请进一步核查这条结论' in page.locator('#message').input_value()
    page.locator('#message').fill('')
    page.locator('#timeline').evaluate('(e)=>e.scrollTop=0')
    page.screenshot(path=str(OUT / 'debate_desktop.png'), full_page=True)
    page.locator('#toggle-minutes').click()
    page.locator('#evidence-filter').select_option('web_summary')
    expect(page.locator('#evidence-list')).to_have_text('尚无匹配证据')
    page.locator('#evidence-filter').select_option('demo')
    expect(page.locator('#evidence-list .evidence-item')).to_have_count(4)
    fixtures=[dict(id='E-'+format(i,'012x'), title='来源 '+str(i), text='资料', locator='测试',date='未提供',source_type='demo') for i in range(75)]
    fixtures[70]['title']='<img src=x onerror=alert(1)>'
    # Mock the paged HTTP response only; never mutate the real meeting store.
    def evidence_page(route):
        number=int(parse_qs(urlparse(route.request.url).query).get('page',['0'])[0])
        route.fulfill(json=dict(items=fixtures[number*30:(number+1)*30], total=75,page=number,size=30))
    page.route('**/evidence?*',evidence_page)
    page.evaluate('renderEvidence()')
    expect(page.locator('#evidence-list .evidence-item')).to_have_count(30)
    page.locator('[data-evidence-page="1"]').click()
    expect(page.locator('#evidence-pagination span')).to_have_text('2 / 3')
    page.locator('[data-evidence-page="1"]').click()
    expect(page.locator('#evidence-list .evidence-item')).to_have_count(15)
    assert page.locator('#evidence-list img').count() == 0
    assert '<img' in page.locator('#evidence-list').inner_text()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(OUT / 'debate_mobile.png'), full_page=True)
    assert not errors, errors
    browser.close()
print('DEBATE BROWSER ACCEPTANCE PASSED')
