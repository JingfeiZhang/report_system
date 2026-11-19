"""Browser acceptance against an already running local discussion server."""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5010'
OUT = ROOT / 'tmp' / 'discussion_browser'
OUT.mkdir(parents=True, exist_ok=True)
CHROME = os.environ.get('DISCUSSION_TEST_CHROME', str(Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1248/chrome-win64/chrome.exe'))


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=CHROME if Path(CHROME).exists() else None)
    page = browser.new_page(viewport={'width': 1440, 'height': 1050}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE + '/discussion')
    page.wait_for_load_state('networkidle')
    page.screenshot(path=str(OUT / 'welcome.png'), full_page=True)
    page.get_by_role('button', name='体验演示案例', exact=True).click()
    page.locator('#meeting-status').filter(has_text='待开始').wait_for()
    page.get_by_role('button', name='开始第一轮 →', exact=True).click()
    page.locator('#meeting-status').filter(has_text='等待你的决策').wait_for(timeout=30000)
    assert page.locator('.card-header').filter(has_text='主持人').count() == 1
    page.locator('.debate-issue .source-tray summary').first.click()
    page.locator('.debate-issue .source-tray [data-evidence]').first.click()
    page.locator('#evidence-dialog[open]').wait_for()
    assert page.locator('#source-text').inner_text()
    page.locator('#evidence-dialog .close-dialog').click()
    page.get_by_role('button', name='填入示例追问 ↗', exact=True).click()
    page.get_by_role('button', name='保存指导 ↑', exact=True).click()
    page.locator('#feedback-list').filter(has_text='下一轮处理').wait_for()
    page.get_by_role('button', name='开始下一轮 →', exact=True).click()
    page.locator('#round-caption').filter(has_text='第 2 轮').wait_for()
    page.locator('#meeting-status').filter(has_text='等待你的决策').wait_for(timeout=30000)
    assert '已有回复' in page.locator('#feedback-list').inner_text()
    page.screenshot(path=str(OUT / 'round_two.png'), full_page=True)
    meeting_url = page.url
    page.reload(wait_until='domcontentloaded')
    page.locator('#round-caption').filter(has_text='第 2 轮').wait_for()
    page.get_by_role('button', name='生成本轮报告', exact=True).click()
    report_link = page.locator('#report-list a')
    report_link.wait_for(timeout=30000)
    report_url = report_link.get_attribute('href')
    report = browser.new_page(viewport={'width': 1100, 'height': 950})
    report.goto(BASE + report_url)
    report.wait_for_load_state('networkidle')
    assert '虚构' in report.locator('article').inner_text()
    report.locator('a.citation').first.click()
    assert report.locator('details:target').count() == 1
    report.screenshot(path=str(OUT / 'report.png'), full_page=True)
    with report.expect_download() as dl:
        report.get_by_role('link', name='下载 Markdown').click()
    dl.value.save_as(OUT / 'download.md')
    assert '60%' in (OUT / 'download.md').read_text(encoding='utf-8')
    phone = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, device_scale_factor=1)
    phone.goto(meeting_url)
    phone.locator('#meeting-title').filter(has_text='星舟').wait_for()
    assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth')
    phone.get_by_role('button', name='纪要与证据', exact=True).click()
    assert phone.locator('#minutes').is_visible()
    phone.keyboard.press('Escape')
    phone.get_by_role('button', name='对话', exact=True).click()
    phone.screenshot(path=str(OUT / 'mobile.png'), full_page=True)
    assert not errors, errors
    (OUT / 'result.json').write_text(json.dumps({'meeting_url': meeting_url, 'report_url': report_url, 'console_errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    browser.close()
print('BROWSER ACCEPTANCE PASSED', OUT)
