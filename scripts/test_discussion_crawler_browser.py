"""Read-only UI acceptance on a real meeting with collected social evidence."""
import argparse
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('meeting')
parser.add_argument('--base', default='http://127.0.0.1:5000')
args = parser.parse_args()
out = Path(__file__).resolve().parents[1] / 'tmp/discussion_browser'
out.mkdir(parents=True, exist_ok=True)
chrome = Path(os.environ.get('DISCUSSION_TEST_CHROME', str(Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright/chromium-1248/chrome-win64/chrome.exe')))
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=str(chrome) if chrome.exists() else None)
    page = browser.new_page(viewport={'width': 1440, 'height': 1050})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(args.base + '/discussion?meeting=' + args.meeting)
    page.locator('#source-settings').filter(has_text='评论样本').wait_for()
    page.locator('.collection-card').wait_for()
    assert page.locator('.collection-card').inner_text().count('已采集') >= 1
    for platform in ['微博', '小红书', '抖音']:
        page.locator('#evidence-search').fill(platform)
        item = page.locator('#evidence-list .evidence-item').filter(has=page.locator('small', has_text=platform)).first
        item.wait_for()
        item.click()
        assert platform in page.locator('#source-meta').inner_text()
        assert '采集：' in page.locator('#source-meta').inner_text()
        assert page.locator('#source-text').inner_text()
        assert page.locator('#source-link').get_attribute('href').startswith('https://')
        page.locator('#evidence-dialog .close-dialog').click()
    page.locator('#evidence-search').fill('')
    page.screenshot(path=str(out/'crawler_live.png'), full_page=True)
    # A running meeting intentionally disables source edits; open only when idle.
    if page.locator('#edit-sources').is_enabled():
        page.locator('#edit-sources').click()
        page.locator('#sources-dialog').wait_for(state='visible')
        assert page.locator('#platform-connections .platform-row').count() == 3
        assert page.locator('#platform-connections input:checked').count() == 3
        page.screenshot(path=str(out/'crawler_settings.png'), full_page=True)
        page.locator('#sources-dialog .close-dialog').click()
        page.set_viewport_size({'width': 390, 'height': 844})
        page.get_by_role('button', name='议程', exact=True).click()
        page.locator('#edit-sources').click()
        page.locator('#sources-dialog').wait_for(state='visible')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(out/'crawler_mobile.png'), full_page=True)
    assert not errors, errors
    browser.close()
print('CRAWLER BROWSER ACCEPTANCE PASSED')
