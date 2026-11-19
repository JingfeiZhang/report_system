"""Read-only acceptance of saved images on a live meeting; never starts analysis."""
import argparse
import hashlib
import json
import os
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:5000')
    parser.add_argument('--meeting', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / 'tmp' / 'image_ui'
    output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.trust_env = False
    api = f'{args.base}/api/discussions/{args.meeting}'
    records = []
    page_number = 0
    while True:
        response = session.get(api + '/evidence', params=dict(source='image', size=100, page=page_number), timeout=15)
        response.raise_for_status()
        payload = response.json()
        records.extend(payload['items'])
        if len(records) >= payload['total']:
            break
        page_number += 1
    saved = next(e for e in records if any(i['status'] == 'downloaded' for i in e['images']))
    failed = next((e for e in records if any(i['status'] == 'failed' for i in e['images'])), None)
    chrome = Path(os.environ.get('DISCUSSION_TEST_CHROME') or str(Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1248/chrome-win64/chrome.exe'))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=str(chrome))
        page = browser.new_page(viewport=dict(width=1440, height=900), accept_downloads=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        # Persistent SSE intentionally prevents networkidle on a real meeting.
        page.goto(f'{args.base}/discussion?meeting={args.meeting}', wait_until='domcontentloaded')
        expect(page.locator('#meeting-status')).not_to_be_empty()
        page.locator('#toggle-minutes').click()
        page.locator('#evidence-filter').select_option('image')
        expect(page.locator('#evidence-list .image-count').first).to_be_visible()

        def open_evidence(evidence):
            for _ in range(1 + len(records) // 30):
                row = page.locator(f'#evidence-list [data-evidence="{evidence["id"]}"]')
                if row.count():
                    row.click()
                    expect(page.locator('#source-images')).to_be_visible()
                    return
                first = page.locator('#evidence-list .evidence-item').first.get_attribute('data-evidence')
                page.locator('[data-evidence-page="1"]').click()
                page.wait_for_function('(old)=>document.querySelector("#evidence-list .evidence-item")?.dataset.evidence!==old', arg=first)
            raise AssertionError('Saved image evidence was not reachable through pagination')

        open_evidence(saved)
        image = page.locator('#source-images img').first
        image.scroll_into_view_if_needed()
        expect(image).to_be_visible()
        page.wait_for_function('document.querySelector("#source-images img")?.naturalWidth>0')
        expect(page.locator('#source-images')).to_contain_text('已有图片识别结果' if any(i.get('analysis_status')=='complete' for i in saved['images']) else '尚未识别图片内容')
        url = image.get_attribute('src')
        sha = url.rsplit('/', 1)[-1]
        with page.expect_popup() as popup:
            image.click()
        popup.value.wait_for_url(args.base + url, wait_until='domcontentloaded')
        popup.value.close()
        with page.expect_download() as pending:
            page.locator(f'#source-images a[href="{url}?download=1"]').click()
        download = pending.value
        assert hashlib.sha256(Path(download.path()).read_bytes()).hexdigest() == sha
        assert download.suggested_filename.startswith(sha + '.')
        page.screenshot(path=str(output / 'desktop.png'))
        page.set_viewport_size(dict(width=390, height=844))
        image.scroll_into_view_if_needed()
        page.screenshot(path=str(output / 'mobile.png'))
        assert page.locator('#evidence-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.keyboard.press('Escape')
        expect(page.locator('#evidence-dialog')).not_to_be_visible()
        expect(page.locator('#insights-dialog')).to_be_visible()
        if failed:
            open_evidence(failed)
            expect(page.locator('#source-images .image-unavailable').first).to_be_visible()
            expect(page.locator('#source-images')).to_contain_text('图片链接拒绝访问或已过期')
            page.keyboard.press('Escape')
        browser.close()
        assert not errors, errors
    result = dict(meeting=args.meeting, evidence_with_images=len(records), preview=True,
                  original_download_sha256=True, mobile_no_overflow=True,
                  failed_download_visible=bool(failed), browser_errors=errors, mutations=0)
    (output / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
