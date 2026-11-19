"""Flask blueprint; no Streamlit or remote database required for discussion."""
import html
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, render_template, request, send_file, stream_with_context
from markdown_it import MarkdownIt

from .evidence import safe_url
from .service import DiscussionService
from .store import Conflict, Store


def render_report(markdown, evidence, source_base=None):
    """Render Markdown with HTML/URL creation disabled; only known citations link."""
    allowed = {e["id"] for e in evidence}
    numbers = {e['id']: i+1 for i, e in enumerate(e for e in evidence if e['id'] in markdown)}
    sources = {e['id']: e for e in evidence}
    def citation_link(eid):
        if eid not in allowed:
            return html.escape(eid)
        target = source_base + eid if source_base else '#source-' + eid
        title = ' · '.join(str(sources[eid].get(k) or '') for k in ('title', 'date')).strip(' ·')
        attrs = ' target="_blank" rel="noopener noreferrer"' if source_base else ''
        return f'<a class="citation" href="{html.escape(target, quote=True)}" title="{html.escape(title, quote=True)}"{attrs}>[来源 {numbers[eid]}]</a>'
    def text_rule(tokens, idx, options, env):
        escaped = html.escape(tokens[idx].content)
        # ReportEngine's superscript notation is not part of CommonMark.
        # Consume only paired markers around evidence references, not prose/math.
        bracket = r'\[\s*E-[a-f0-9]{12}(?:\s*[,，、;；]\s*E-[a-f0-9]{12})*\s*\]'
        escaped = re.sub(r'\^((?:'+bracket+r'\s*)+)\^', r'\1', escaped)
        # CommonMark treats a CJK label ending in punctuation followed directly
        # by CJK prose as literal stars. Format that label after HTML escaping.
        escaped = re.sub(r'\*\*([^*\n]{1,100}[：:])\*\*', r'<strong>\1</strong>', escaped)
        def citation(match):
            ids = re.findall(r'E-[a-f0-9]{12}', match[0])
            return ' '.join(citation_link(eid) for eid in ids)
        return re.sub(bracket+r'|E-[a-f0-9]{12}', citation, escaped)
    parser = MarkdownIt("commonmark", {"html": False}).enable("table").disable(["link", "image", "autolink"])
    parser.renderer.rules["text"] = text_rule
    def code_rule(tokens, idx, options, env):
        text = tokens[idx].content
        ids = re.findall(r'E-[a-f0-9]{12}', text)
        remainder = re.sub(r'E-[a-f0-9]{12}', '', text)
        # Adjacent Markdown code spans can be parsed as one span containing
        # backticks. Accept only citation punctuation; never arbitrary code/HTML.
        if ids and re.fullmatch(r'[\[\]`\s,，;；]*', remainder):
            return ' '.join(citation_link(eid) for eid in ids)
        return "<code>" + html.escape(text) + "</code>"
    parser.renderer.rules["code_inline"] = code_rule
    return parser.render(markdown)


def register_discussion(app, root=None, provider=None):
    root = root or os.environ.get("DISCUSSION_DATA_DIR") or str(Path(app.root_path) / "discussion_data")
    store = Store(root)
    store.recover()
    service = DiscussionService(store, provider)
    app.extensions["discussion"] = service
    bp = Blueprint("discussion", __name__)

    def data():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("请求必须为 JSON 对象")
        return value

    def output(state):
        return jsonify(service.public(state))

    @bp.errorhandler(Conflict)
    def conflict(exc):
        return jsonify(error=str(exc)), 409

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @bp.errorhandler(KeyError)
    def missing(exc):
        return jsonify(error="未找到会议或记录"), 404

    @bp.route("/discussion")
    def page():
        return render_template("discussion.html")

    @bp.route("/api/discussions", methods=["GET", "POST"])
    def meetings():
        if request.method == "GET":
            return jsonify(items=store.list())
        return output(service.create(data())), 201

    @bp.route("/api/discussions/<mid>", methods=["GET", "PATCH"])
    def meeting(mid):
        if request.method == "PATCH":
            return output(service.update_agenda(mid, data()))
        return output(store.get(mid, summary=True))

    @bp.get('/api/discussions/<mid>/evidence')
    def evidence_page(mid):
        store.get(mid, summary=True)
        return jsonify(store.evidence_page(mid, request.args.get('page', 0), request.args.get('size', 30),
            request.args.get('q', ''), request.args.get('source', ''), request.args.get('platform', '')))

    @bp.get('/api/discussions/<mid>/evidence/<eid>')
    def evidence_item(mid, eid):
        return jsonify(store.evidence_item(mid, eid))

    @bp.post('/api/discussions/<mid>/images/collect')
    def collect_images(mid):
        return output(service.collect_images(mid)), 202

    @bp.post('/api/discussions/<mid>/images/stop')
    def stop_images(mid):
        return output(service.stop_images(mid)), 202

    @bp.post('/api/discussions/<mid>/images/analyze')
    def analyze_images(mid):
        return output(service.analyze_images(mid, data())), 202

    @bp.post('/api/discussions/<mid>/images/analysis/stop')
    def stop_image_analysis(mid):
        return output(service.stop_image_analysis(mid)), 202

    @bp.get('/api/discussions/<mid>/images/<sha>')
    def image_file(mid, sha):
        from .images import attachment_path
        store.get(mid, summary=True)
        original = request.args.get('download') == '1'
        path = attachment_path(store.root, mid, sha, original)
        suffix = '.jpg'
        if original:
            from PIL import Image
            with Image.open(path) as picture:
                suffix = {'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif', 'WEBP': '.webp', 'AVIF': '.avif'}.get(picture.format, '.original')
        response = send_file(path, mimetype='application/octet-stream' if original else 'image/jpeg',
                             as_attachment=original, download_name=sha + suffix,
                             conditional=True)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @bp.get('/api/discussions/<mid>/collections')
    def collections(mid):
        from .collection import public_task
        store.get(mid, summary=True)
        items = [public_task(t) for t in store.collection_tasks(mid)]
        for key in ('round', 'platform', 'status'):
            value = request.args.get(key)
            if value:
                items = [t for t in items if str(t.get(key)) == value]
        page = max(0, int(request.args.get('page', 0)))
        size = max(1, min(100, int(request.args.get('size', 30))))
        return jsonify(items=items[page*size:(page+1)*size], total=len(items), page=page, size=size)

    @bp.post('/api/discussions/<mid>/collections/stop')
    def stop_collection(mid):
        return output(service.stop_collection(mid)), 202

    @bp.post('/api/discussions/<mid>/collections/resume')
    def resume_collection(mid):
        return output(service.resume_collection(mid, data())), 202

    @bp.post("/api/discussions/<mid>/plan")
    def plan(mid):
        return output(service.plan(mid)), 202

    @bp.post('/api/discussions/<mid>/stop')
    def stop(mid):
        return output(service.stop_round(mid, (request.get_json(silent=True) or {}).get('action', 'finish_exchange'))), 202

    @bp.post('/api/discussions/<mid>/rounds/resume')
    def resume_round(mid):
        return output(service.resume_round(mid)), 202

    @bp.post('/api/discussions/<mid>/rounds/remediate')
    def remediate_round(mid):
        return output(service.remediate_round(mid, data())), 202

    @bp.patch("/api/discussions/<mid>/sources")
    def sources(mid):
        return output(service.update_sources(mid, data()))

    @bp.get("/api/discussion-crawler/connections")
    def crawler_connections():
        return jsonify(items=service.crawler.connections())

    @bp.post("/api/discussion-crawler/<platform>/login")
    def crawler_login(platform):
        # The login window belongs to the machine running Flask, not a remote visitor.
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify(error="请在运行项目的电脑上打开工作台进行平台登录"), 403
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify(error="请从本地工作台发起登录"), 403
        body = data()
        return jsonify(items=service.crawler.login(platform, finish=body.get("action") == "finish", query=str(body.get("query", ""))[:120])), 202

    @bp.post("/api/discussions/<mid>/messages")
    def message(mid):
        return output(service.message(mid, data())), 201

    @bp.post("/api/discussions/<mid>/materials")
    def materials(mid):
        store.get(mid)
        if request.content_length and request.content_length > 11 * 1024 * 1024:
            return jsonify(error="单个文件不超过 10 MB"), 413
        upload = request.files.get("file")
        if not upload or not upload.filename:
            raise ValueError("请选择文件")
        return output(service.upload(mid, upload.filename, upload.stream.read(10 * 1024 * 1024 + 1))), 201

    @bp.post("/api/discussions/<mid>/rounds")
    def rounds(mid):
        return output(service.start_round(mid, data())), 202

    @bp.post("/api/discussions/<mid>/reports")
    def reports(mid):
        return output(service.report(mid, data())), 202

    @bp.get("/api/discussions/<mid>/events")
    def events(mid):
        store.get(mid, summary=True)
        try:
            cursor = max(0, int(request.headers.get("Last-Event-ID") or request.args.get("after", 0)))
        except ValueError:
            raise ValueError("事件序号无效") from None
        @stream_with_context
        def stream():
            nonlocal cursor
            sleeper = threading.Event()
            while True:
                rows = store.events(mid, cursor)
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                    sleeper.wait(2)
        return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @bp.get("/discussions/<mid>/reports/<rid>")
    def report_view(mid, rid):
        state = store.get(mid)
        report = next((r for r in state["reports"] if r["id"] == rid and r["status"] == "complete"), None)
        if not report:
            raise KeyError(rid)
        snapshot = next(s for s in state["snapshots"] if s["id"] == report["snapshot_id"])
        path = store.root / mid / rid / "report.md"
        if request.args.get("format") == "md":
            return send_file(path, as_attachment=True, download_name=f"discussion-round-{report['round']}.md", mimetype="text/markdown")
        md = path.read_text(encoding="utf-8")
        evidence = [dict(e, url=safe_url(e["url"])) for e in snapshot["evidence"] if e["id"] in md]
        event_only = report.get('generation_version', 1) >= 3
        source_base = f'/discussions/{mid}/reports/{rid}/sources/' if event_only else None
        response = Response(render_template("discussion_report.html", meeting=state, report=report,
                                            body=render_report(md, snapshot["evidence"], source_base),
                                            evidence=evidence, event_only=event_only))
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'self'"
        return response

    @bp.get('/discussions/<mid>/reports/<rid>/sources/<eid>')
    def report_source(mid, rid, eid):
        state = store.get(mid)
        report = next((r for r in state['reports'] if r['id'] == rid and r['status'] == 'complete'), None)
        if not report:
            raise KeyError(rid)
        snapshot = next(s for s in state['snapshots'] if s['id'] == report['snapshot_id'])
        evidence = next((e for e in snapshot['evidence'] if e['id'] == eid), None)
        markdown = (store.root / mid / rid / 'report.md').read_text(encoding='utf-8')
        if not evidence or eid not in markdown:
            raise KeyError(eid)
        parent = next((e for e in snapshot['evidence'] if e['id'] == evidence.get('parent_evidence_id')), None)
        response = Response(render_template('discussion_report_source.html', meeting=state, report=report,
                                           parent=parent,
                                           evidence=dict(evidence, url=safe_url(evidence.get('url', '')))))
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'self'"
        return response

    app.register_blueprint(bp)
    return service

