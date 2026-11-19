"""Read-only acceptance of a saved remediation, its history and conversation UI."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from discussion.checkpoints import digest
from discussion.store import Store
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('meeting')
    parser.add_argument('--before', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    state = Store(ROOT / 'discussion_data').get(args.meeting)
    before = json.loads(Path(args.before).read_text(encoding='utf-8'))
    preserved = {key: digest(state[key][:value['count']]) == value['hash'] for key, value in before.items()}
    assert all(preserved.values()), preserved
    r = state['rounds'][-1]
    meta = r['remediation']
    assert state['status'] == 'waiting' and r['status'] == 'complete' and meta['status'] == 'complete'
    assert all(t['status'] == 'complete' for t in r['tasks'])
    assert not r['verification_summary']['pending']
    snapshot = next(s for s in state['snapshots'] if s['round'] == r['number'])
    source = next(s for s in state['snapshots'] if s['id'] == meta['source_snapshot_id'])
    assert snapshot['evidence'] == source['evidence']
    assert snapshot['sentiment'] == source['sentiment']
    assert len(state['reports']) == before['reports']['count']
    for mapping in meta['results']:
        assert any(t['id'] == mapping['task_id'] and t.get('source_task_id') == mapping['source_task_id'] for t in r['tasks'])
        assert any(c['id'] == mapping['card_id'] and c['task_id'] == mapping['task_id'] for c in r['cards'])
    challenges = [c for review in r['peer_reviews'] for c in review['challenges']]
    assert all(c.get('reply') and c.get('verification') for c in challenges)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path='C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1248/chrome-win64/chrome.exe')
        page = browser.new_page(viewport=dict(width=1440, height=900))
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:5000/discussion?meeting={args.meeting}', wait_until='domcontentloaded')
        expect(page.locator('#meeting-status')).to_have_text('等待你的决策')
        expect(page.locator('.round-divider')).to_contain_text(f"第 {meta['source_round']} 轮补救")
        assert page.locator('.issue-failure').count() == 0
        assert page.locator('.reply-message').count() == len(challenges)
        page.locator('#round-summary').scroll_into_view_if_needed()
        page.evaluate("const t=document.getElementById('timeline');t.scrollTop+=document.getElementById('round-summary').getBoundingClientRect().top-t.getBoundingClientRect().top-12")
        page.screenshot(path=str(out / 'summary.png'))
        page.locator(f"#round-list [data-round='{meta['source_round']}']").click()
        expect(page.locator('.remediation-link')).to_have_count(len(r['issues']))
        expect(page.locator('#round-summary .tag')).to_contain_text('已补救')
        page.locator(f".remediation-link [data-round='{r['number']}']").first.click()
        expect(page.locator('.round-divider')).to_contain_text('补救')
        for width, height in [(1440, 900), (1280, 800), (390, 844)]:
            page.set_viewport_size(dict(width=width, height=height))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.set_viewport_size(dict(width=1440, height=900))
        repaired_card = page.locator(f"[data-card-id='{meta['results'][0]['card_id']}']")
        repaired_card.scroll_into_view_if_needed()
        page.screenshot(path=str(out / 'revisions.png'))
        assert not errors, errors
        browser.close()
    result = dict(meeting=state['id'], round=r['number'], source_round=meta['source_round'],
        snapshot_id=snapshot['id'], status='complete', source_history_unchanged=preserved,
        repaired_tasks=meta['results'], task_counts=dict(Counter(t['phase'] for t in r['tasks'])),
        verification_summary=r['verification_summary'], challenge_count=len(challenges),
        challenge_statuses=dict(Counter(c['status'] for c in challenges)),
        fresh_verified_claims=len(r['verifications']),
        evidence_count=len(snapshot['evidence']), evidence_unchanged=True, sentiment_unchanged=True,
        reports_unchanged=True, page_errors=errors,
        tasks=[{k: t.get(k) for k in ('id','role','phase','agent_id','status','attempts','elapsed_seconds')}
               for t in r['tasks']])
    (out / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('tasks','repaired_tasks')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
