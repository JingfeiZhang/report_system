"""Conversation UI regression with an isolated demo database; no paid calls."""
import json
import os
import tempfile
import threading
from pathlib import Path

from test_discussion import FakeProvider, InlineExecutor
from flask import Flask
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
from discussion.api import register_discussion

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tmp' / 'chat_ui'
OUT.mkdir(parents=True, exist_ok=True)
CHROME = Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1248/chrome-win64/chrome.exe'

with tempfile.TemporaryDirectory() as directory:
    app = Flask(__name__, root_path=str(ROOT), template_folder='templates', static_folder='static')
    service = register_discussion(app, directory, FakeProvider())
    service.pool.shutdown()
    service.pool = InlineExecutor()
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=str(CHROME))
            page = browser.new_page(viewport=dict(width=1440, height=900))
            # Full reciprocal rounds persist more calls than the earlier single-role demo.
            expect.set_options(timeout=15000)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + '/discussion', wait_until='networkidle')
            page.screenshot(path=str(OUT / 'welcome.png'))
            page.locator('#quick-demo').click()
            expect(page.locator('#meeting-status')).to_have_text('待开始')
            mid = page.url.split('meeting=')[1]
            page.locator('#start-round').click()
            expect(page.locator('#meeting-status')).to_have_text('等待你的决策')
            expect(page.locator('.debate-issue .answer-message').first).to_be_visible()
            assert page.locator('.answer-message.left').count() > 0
            assert page.locator('.answer-message.right').count() > 0
            assert page.locator('.challenge-message').count() > 0
            assert page.locator('.reply-message').count() > 0
            assert page.locator('#round-summary').count() == 1
            for role in ('facts', 'media', 'sentiment'):
                issue = page.locator(f'.debate-issue[data-role="{role}"]')
                expect(issue.locator('.collaboration-details summary')).to_contain_text('双向互审')
                assert issue.locator('.answer-message.left').count() > 0
                assert issue.locator('.answer-message.right').count() > 0
                assert issue.locator('.challenge-message').count() > 0
                assert issue.locator('.reply-message').count() > 0
            assert '报道关注点不同' in page.locator('.debate-issue[data-role="media"]').inner_text()
            assert '程序一致的6/10口径' in page.locator('.debate-issue[data-role="sentiment"]').text_content()

            # Reply links return to their actual persisted challenge.
            page.locator('.reply-link').first.click()
            expect(page.locator('.highlight-message')).to_be_visible()
            page.locator('[data-jump="conversation"]').click()
            assert page.locator('[data-view], [data-jump="latest"], [data-jump="challenges"]').count() == 0
            assert '独立答案' in page.locator('.answer-message').first.locator('.message-meta').inner_text()
            page.locator('.claim-details summary').first.click()
            page.locator('[data-ask-claim]').first.click()
            assert '请进一步核查这条结论' in page.locator('#message').input_value()
            # A source still opens from a collapsed tray or an inline citation.
            citation = page.locator('.claim-details[open] [data-evidence]').first
            evidence_id = citation.get_attribute('data-evidence')
            evidence_item = service.store.evidence_item(mid, evidence_id)
            citation_number = citation.locator('.citation-number').inner_text()
            expect(citation.locator('.citation-title')).to_have_text(evidence_item['title'])
            expect(citation.locator('.citation-kind')).to_have_text('演示')
            assert '查看来源' not in citation.inner_text()
            numbers = page.locator(f'.citation[data-evidence="{evidence_id}"] .citation-number').all_text_contents()
            assert all(n == citation_number for n in numbers), (citation_number, numbers)
            citation.click()
            expect(page.locator('#source-text')).not_to_be_empty()
            expect(page.locator('#source-title')).to_contain_text(evidence_item['title'])
            expect(page.locator('#source-meta')).to_contain_text('虚构演示资料')
            expect(page.locator('#source-meta')).to_contain_text(evidence_id)
            page.keyboard.press('Escape')
            assert not page.locator('#evidence-dialog').evaluate('(e)=>e.open')
            # Sources outside the meeting's first page resolve lazily, once per ID.
            requests = []
            page.on('request', lambda request: requests.append(request.url) if request.url.endswith('/evidence/' + evidence_id) else None)
            page.evaluate('''id=>{state.evidence=state.evidence.filter(e=>e.id!==id);
                evidenceCache.delete(id);lastSignature='';render();}''', evidence_id)
            expect(citation.locator('.citation-title')).to_have_text(evidence_item['title'])
            assert len(requests) == 1, requests
            assert citation.locator('.citation-number').inner_text() == citation_number

            # Basic chat keyboard behavior, including Chinese IME and error recovery.
            page.locator('#message').fill('')
            expect(page.locator('#send-message')).to_be_disabled()
            page.locator('#target').select_option('facts')
            page.locator('#target').select_option('all')
            page.locator('#message').fill('第一行')
            page.locator('#message').press('Shift+Enter')
            page.locator('#message').press('a')
            expect(page.locator('#message')).to_have_value('第一行\na')
            page.locator('#message').dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
            assert len(service.store.get(mid)['messages']) == 0
            page.route('**/api/discussions/*/messages', lambda route: route.fulfill(
                status=503, content_type='application/json', body=json.dumps({'error': '发送失败，请重试'})))
            page.locator('#message').fill('发送失败时保留消息')
            page.locator('#message').press('Enter')
            expect(page.locator('#toast')).to_have_text('发送失败，请重试')
            expect(page.locator('#message')).to_have_value('发送失败时保留消息')
            expect(page.locator('#send-message')).to_be_enabled()
            assert len(service.store.get(mid)['messages']) == 0
            page.unroute('**/api/discussions/*/messages')

            # Sending guidance shows it after this round, without launching analysis.
            page.locator('#demo-followup').click()
            page.locator('#message').press('Enter')
            expect(page.locator('#message')).to_have_value('')
            assert len(service.store.get(mid)['rounds']) == 1
            assert service.store.get(mid)['messages'][-1]['status'] == 'pending'
            expect(page.locator('#timeline > .user-card')).to_have_count(1)
            assert page.locator('#timeline > .user-card').evaluate('(e)=>!e.closest("details")')
            expect(page.locator('#round-summary ~ .user-card')).to_contain_text('下一轮处理')
            assert page.locator('.user-card').bounding_box()['y'] < page.locator('#timeline').bounding_box()['y'] + page.locator('#timeline').bounding_box()['height']
            page.locator('#start-round').click()
            expect(page.locator('#round-caption')).to_contain_text('第 2 轮')
            expect(page.locator('#meeting-status')).to_have_text('等待你的决策')
            assert service.store.get(mid)['messages'][-1]['status'] == 'answered'
            assert len(service.store.get(mid)['rounds']) == 2
            expect(page.locator('#timeline > .user-card')).to_contain_text('已有回复')
            assert page.locator('#timeline > .user-card').evaluate('(e)=>Boolean(e.compareDocumentPosition(document.querySelector(".debate-issue")) & Node.DOCUMENT_POSITION_FOLLOWING)')

            page.locator('[data-jump="summary"]').click()
            page.locator('[data-next-question]').first.click()
            assert page.locator('#message').input_value()
            assert len(service.store.get(mid)['messages']) == 1
            page.locator('#message').fill('')
            page.locator('#generate-report').click()
            page.locator('#report-list a').wait_for()
            assert service.store.get(mid)['reports'][-1]['status'] == 'complete'

            # Drawers restore focus, keyboard dismissal, and source pagination.
            page.locator('#toggle-minutes').click()
            expect(page.locator('#insights-dialog')).to_be_visible()
            page.locator('#evidence-filter').select_option('demo')
            expect(page.locator('#evidence-list .evidence-item')).to_have_count(4)
            page.keyboard.press('Escape')
            expect(page.locator('#toggle-minutes')).to_be_focused()

            # Progress-only events never replace or scroll the conversation.
            page.locator('[data-jump="conversation"]').click()
            page.evaluate("""stream?.close(); $('timeline').scrollTop=90;
                window.beforeHtml=$('timeline').innerHTML;window.beforeScroll=$('timeline').scrollTop;
                state.rounds.at(-1).collection_budget={mode:'adaptive',used_seconds:800,active:true,request_count:10};render();""")
            assert page.evaluate("$('timeline').innerHTML===beforeHtml && $('timeline').scrollTop===beforeScroll")

            # Untrusted text is displayed verbatim and cannot become executable HTML.
            page.evaluate("""state.rounds.at(-1).cards.find(c=>c.phase==='proposal').conclusion='<img src=x onerror=alert(1)> [E-aaaaaaaaaaaa]';render();""")
            assert page.locator('#timeline img').count() == 0
            assert '<img' in page.locator('#timeline').inner_text()
            # A legacy single-agent issue must not be presented as an agreement.
            page.evaluate("""const i=state.rounds.at(-1).issues[0];i.mode='single';
                state.rounds.at(-1).peer_reviews=state.rounds.at(-1).peer_reviews.filter(r=>r.issue_id!==i.id);
                render();""")
            expect(page.locator('.debate-issue').first.locator('.collaboration-details summary')).to_contain_text('专项分析')
            expect(page.locator('.debate-issue').first.locator('.collaboration-details')).to_contain_text('没有相互质疑环节')
            # A completed round with a failed proposal must never look still running.
            page.evaluate('''const media={...state.rounds[0].issues.find(i=>i.role==='media'),
                    id:'failed-issue-media',agents:['failed-media-agent'],status:'failed',mode:'single',stop_reason:'没有有效提案'};
                state.rounds.at(-1).issues.push(media);
                state.rounds.at(-1).tasks.push({id:'failed-media',issue_id:media.id,agent_id:media.agents[0],
                    phase:'proposal',role:'media',status:'failed',error_type:'timeout',error:'模型请求失败（APITimeoutError）',attempts:2,elapsed_seconds:200.28});render();''')
            failed_issue = page.locator('.debate-issue[data-role="media"]')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('模型请求超时')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('已尝试 2 次')
            assert '正在准备材料' not in failed_issue.inner_text()
            page.evaluate('''Object.assign(state.rounds.at(-1).tasks.at(-1),{phase:'revision',exchange:2,
                error:'修订未逐项回应对方质询，保留上一版并重试失败任务',error_type:'execution_error'});render();''')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('第 2 次交锋')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('回应校验未通过')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('仍保留此前的有效答案')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('第 2 轮记录')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('此历史任务未保存具体缺失项')
            expect(page.locator('#round-summary .tag')).to_contain_text('1 项任务未完成')
            failed_issue.locator('[data-retry-issue]').click()
            expect(page.locator('#target')).to_have_value('media')
            assert '请重新分析' in page.locator('#message').input_value()
            page.evaluate('''state.rounds.at(-1).peer_reviews.push({issue_id:'failed-issue-media',exchange:2,
                author:'failed-media-agent',target_agent:'failed-media-agent',agreements:[],complete:true,
                challenges:[{id:'pending-ui-question',target_agent:'failed-media-agent',question:'转载能否算独立来源？',
                    reason:'来源可能重复',resolution:'核对原始出处',status:'open'}]});
                state.rounds.at(-1).tasks.at(-1).reply_repair={total:2,completed:1,
                    pending:[{id:'pending-ui-question',question:'转载能否算独立来源？',resolution:'核对原始出处'}]};render();''')
            expect(failed_issue.locator('.issue-failure')).to_contain_text('单项补答已保存 1/2 项')
            failed_issue.locator('[data-retry-issue]').click()
            assert '转载能否算独立来源？' in page.locator('#message').input_value()
            assert len(service.store.get(mid)['rounds']) == 2
            # A separate completed remediation links the historical failure;
            # it must not mutate that original failed task or show it as pending.
            page.evaluate('''selectedRound=2;const repair=structuredClone(state.rounds[1]);
                repair.number=3;repair.tasks=[];repair.remediation={status:'complete',source_round:2,
                    source_task_ids:['failed-media'],results:[{source_task_id:'failed-media'}]};
                state.rounds.push(repair);render();''')
            expect(failed_issue.locator('.remediation-link')).to_contain_text('完成修订与核验')
            expect(page.locator('#round-summary .tag')).to_contain_text('已补救')
            assert page.evaluate("state.rounds[1].tasks.at(-1).status") == 'failed'
            failed_issue.locator('.remediation-link [data-round="3"]').click()
            expect(page.locator('.round-divider')).to_contain_text('第 2 轮补救')
            page.evaluate("sessionStorage.removeItem('discussion-view-'+state.id)")
            page.reload(wait_until='domcontentloaded')
            page.locator('.debate-issue .answer-message').first.wait_for()

            for width, height in [(1440,900), (1280,800), (390,844)]:
                page.set_viewport_size(dict(width=width, height=height))
                page.evaluate('sessionStorage.clear()')
                page.reload(wait_until='domcontentloaded')
                page.locator('.debate-issue .answer-message').first.wait_for()
                expect(page.locator('#start-round')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                bounds = page.locator('#timeline').bounding_box()
                prose = page.locator('#timeline .answer-prose').first.bounding_box()
                assert prose['y'] + 35 < bounds['y'] + bounds['height'], (width, bounds, prose)
                assert page.locator('#toggle-guidance, .guidance-history').count() == 0
                expect(page.locator('#message')).to_be_visible()
                expect(page.locator('#send-message')).to_be_disabled()
                page.locator('#theme-toggle').click()
                assert page.locator('html').get_attribute('data-theme') == 'light'
                page.locator('#theme-toggle').click()
                page.screenshot(path=str(OUT / f'demo-{width}.png'))
                if width < 900:
                    page.locator('[data-panel="minutes"]').click()
                    expect(page.locator('#insights-dialog')).to_be_visible()
                    page.keyboard.press('Escape')
                    page.locator('[data-panel="navigation"]').click()
                page.locator('[data-round="1"]').click()
                expect(page.locator('#round-caption')).to_contain_text('第 1 轮')
                page.reload(wait_until='domcontentloaded')
                expect(page.locator('#round-caption')).to_contain_text('第 1 轮')
            assert not errors, errors
            (OUT / 'acceptance.json').write_text(json.dumps(dict(
                result='passed', viewports=['1440x900','1280x800','390x844'],
                checks=['independent speakers', 'challenge links', 'conversation only', 'citations',
                        'inline guidance', 'Enter send', 'Shift+Enter newline', 'IME guard',
                        'failed send preserves draft', 'guidance pause', 'second round', 'report', 'drawer focus',
                        'progress preserves reading', 'escaped content', 'theme', 'round restore',
                        'named source labels', 'lazy source lookup', 'failed proposal display'],
                page_errors=errors), ensure_ascii=False, indent=2), encoding='utf-8')
            browser.close()
    finally:
        server.shutdown()
        thread.join()
print('CHAT UI ACCEPTANCE PASSED')
