"""Read-only UI regression; synthetic progress is confined to the browser state."""
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]
BASE = sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:5000'
MID = sys.argv[2] if len(sys.argv)>2 else 'meeting-3752f5514e09'
OUT = ROOT / 'tmp' / 'reading_workspace'
OUT.mkdir(parents=True, exist_ok=True)
CHROME = os.environ.get('DISCUSSION_TEST_CHROME', str(Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1248/chrome-win64/chrome.exe'))
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=CHROME)
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    for width,height in [(1440,900),(1280,800),(390,844)]:
        if page.url.startswith(BASE):
            page.evaluate('sessionStorage.clear()')
        page.set_viewport_size(dict(width=width,height=height))
        page.goto(BASE+'/discussion?meeting='+MID)
        page.wait_for_selector('.debate-issue .answer-message')
        page.wait_for_function("$('timeline').scrollTop>0")
        page.evaluate("stream?.close()")
        page.wait_for_load_state('networkidle')
        bounds = page.locator('#timeline').bounding_box()
        answer = page.locator('.debate-issue .card').first.bounding_box()
        assert answer['y'] < bounds['y']+bounds['height'], (width,bounds,answer)
        prose = page.locator('.debate-issue .answer-prose').first.bounding_box()
        assert prose['y']+35 < bounds['y']+bounds['height'], (width,bounds,prose)
        if width<=1280:
            page.locator('#toggle-guidance').click()
            assert not page.locator('#message').is_visible()
            page.locator('#toggle-guidance').click()
            assert page.locator('#message').is_visible()
        assert page.locator('#collection-summary').bounding_box()['height']<=80
        page.screenshot(path=str(OUT/f'workspace-{width}.png'))
        page.locator('#open-collection').click()
        page.wait_for_selector('#collection-items details')
        assert page.locator('#collection-items details[open]').count()==0
        assert page.locator('#collection-items > details').count()<=12
        page.keyboard.press('Escape')
        assert not page.locator('#collection-dialog').evaluate('(e)=>e.open')
        page.locator('[data-jump="conversation"]').click()
        page.evaluate("$('timeline').scrollTop=80; const d=$('timeline').querySelector('details'); if(d)d.open=true; window.savedTimeline=$('timeline').innerHTML; window.savedScroll=$('timeline').scrollTop; state.rounds.at(-1).collection_budget.remaining_seconds=10; render()")
        assert page.evaluate("$('timeline').innerHTML===window.savedTimeline && $('timeline').scrollTop===window.savedScroll")
        page.locator('[data-jump="summary"]').click()
        print(width, 'answer visible, drawer, keyboard, progress preserves reading: OK')
    assert not errors, errors
    browser.close()
