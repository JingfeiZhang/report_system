"""Read-only live UI checks for recognized images and their original posts."""
import argparse
import hashlib
import json
import os
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--meeting', required=True)
    parser.add_argument('--base', default='http://127.0.0.1:5000')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root/'tmp'/'vision_ui'
    out.mkdir(parents=True, exist_ok=True)
    s = requests.Session(); s.trust_env = False
    url = f'{args.base}/api/discussions/{args.meeting}'
    response = s.get(url+'/evidence', params=dict(source='image_analysis'), timeout=15)
    response.raise_for_status()
    extraction = response.json()['items'][0]
    image_id = extraction['image_id']
    chrome = os.environ.get('DISCUSSION_TEST_CHROME') or str(Path(os.environ['LOCALAPPDATA'])/'ms-playwright/chromium-1248/chrome-win64/chrome.exe')
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=chrome)
        page = browser.new_page(viewport=dict(width=1440, height=900), accept_downloads=True)
        page.on('pageerror', lambda e:errors.append(str(e)))
        # Close the endless event stream only in this read-only UI test.
        page.route('**/events*', lambda route:route.fulfill(status=200, content_type='text/event-stream', body=''))
        page.goto(f'{args.base}/discussion?meeting={args.meeting}', wait_until='networkidle')
        expect(page.locator('#meeting-status')).not_to_be_empty()
        page.locator('#toggle-minutes').click()
        page.locator('#evidence-filter').select_option('image_analysis')
        item = page.locator(f'#evidence-list [data-evidence="{extraction["id"]}"]')
        expect(item).to_be_visible()
        item.click()
        expect(page.locator('#source-meta')).to_contain_text('图片机器提取')
        expect(page.locator('#source-text')).to_contain_text('图片文字：')
        expect(page.locator('#source-text')).to_contain_text('可见信息：')
        page.screenshot(path=str(out/'recognized-desktop.png'))
        page.locator('#image-parent').click()
        expect(page.locator('#source-meta')).to_contain_text('帖子文字')
        link = page.locator(f'[data-extraction="{extraction["id"]}"]')
        expect(link).to_be_visible()
        preview = page.locator('#source-images img').first
        preview.scroll_into_view_if_needed()
        page.wait_for_function('document.querySelector("#source-images img")?.naturalWidth>0')
        image_url = preview.get_attribute('src')
        with page.expect_download() as pending:
            page.locator(f'#source-images a[href="{image_url}?download=1"]').click()
        download = pending.value
        assert hashlib.sha256(Path(download.path()).read_bytes()).hexdigest() == extraction['image_sha256']
        link.click()
        expect(page.locator('#image-parent')).to_be_visible()
        page.set_viewport_size(dict(width=390, height=844))
        page.locator('#source-text').scroll_into_view_if_needed()
        expect(page.locator('#source-text')).to_be_visible()
        assert page.locator('#evidence-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(out/'recognized-mobile.png'))
        page.keyboard.press('Escape')
        expect(page.locator('#evidence-dialog')).not_to_be_visible()
        browser.close()
    assert not errors, errors
    result = dict(meeting=args.meeting, evidence=extraction['id'], image_id=image_id,
                  recognized_text_visible=True, parent_roundtrip=True, original_hash_verified=True,
                  mobile_no_overflow=True, browser_errors=errors, mutations=0)
    (out/'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
