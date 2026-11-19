"""Reuse successful ReportEngine stages for the same frozen input and model."""
from dataclasses import asdict
from pathlib import Path
import time

from . import checkpoints


def install(agent, folder, identity, progress):
    root = Path(folder) / 'generation-cache'
    layout_run = agent.document_layout_node.run
    chapter_run = agent.chapter_generation_node.run

    def execute(name, payload, run, valid=lambda value: True):
        fingerprint = checkpoints.digest(['report-stage-v1', identity, payload])
        path = root / (name + '-' + fingerprint + '.json')
        cached = checkpoints.read(path)
        if cached:
            progress('复用已完成报告阶段：' + name)
            return cached['result']
        start = time.time()
        try:
            value = run()
            if valid(value):
                checkpoints.write(path, dict(result=value, started_at=start,
                                             elapsed_seconds=round(time.time()-start, 3)))
            return value
        except Exception as exc:
            checkpoints.write(root / (name + '-failure.json'), dict(
                fingerprint=fingerprint, started_at=start,
                elapsed_seconds=round(time.time()-start, 3), error_type=type(exc).__name__))
            raise

    def layout(sections, *args):
        return execute('layout', [[asdict(s) for s in sections], args],
                       lambda: layout_run(sections, *args),
                       lambda value: isinstance(value, dict) and all(k in value for k in
                           ('title', 'hero', 'tocPlan', 'tocTitle')))

    def chapter(section, context, run_dir, **kwargs):
        result = execute(section.chapter_id, [asdict(section), context],
                         lambda: chapter_run(section, context, run_dir, **kwargs))
        # A fresh engine session resets its manifest. Register reused chapters too.
        meta = dict(chapterId=section.chapter_id, slug=section.slug,
                    title=section.title, order=section.order)
        agent.chapter_storage.persist_chapter(run_dir, meta, result)
        return result

    agent.document_layout_node.run = layout
    agent.chapter_generation_node.run = chapter
