"""Backfill saved social-post pictures without search or model requests."""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discussion.store import Store
from discussion.service import DiscussionService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--meeting', required=True)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / 'discussion_data')
    args = parser.parse_args()
    store = Store(args.root)
    state = store.get(args.meeting, summary=True)
    if state['status'] in ('running', 'planning', 'reporting', 'collecting_images'):
        raise SystemExit('会议任务正在执行，待其结束后再补取图片')
    backup = args.root / 'backups' / ('before-images-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3')
    backup.parent.mkdir(parents=True, exist_ok=True)
    with store.connect() as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    def frozen_digest():
        with store.connect() as conn:
            rows = list(conn.execute('SELECT payload FROM snapshot_data WHERE meeting=? ORDER BY id', (args.meeting,)))
            links = list(conn.execute('SELECT snapshot,kind,id,version FROM snapshot_documents WHERE meeting=? ORDER BY snapshot,kind,id', (args.meeting,)))
        return hashlib.sha256(repr(([tuple(r) for r in rows], [tuple(r) for r in links])).encode()).hexdigest()
    before = frozen_digest()
    service = DiscussionService(store)
    service.collect_images(args.meeting)
    last = None
    try:
        while True:
            current = store.get(args.meeting, summary=True)
            info = current.get('image_collection', {})
            if info.get('message') != last:
                print(info.get('message', ''), flush=True)
                last = info.get('message')
            if info.get('status') != 'running':
                break
            time.sleep(2)
    except KeyboardInterrupt:
        service.stop_images(args.meeting)
    finally:
        service.pool.shutdown(wait=True)
    state = store.get(args.meeting)
    pictures = [i for e in state['evidence'] for i in e.get('images', [])]
    result = dict(meeting=args.meeting, operation=state.get('image_collection'),
                  saved=sum(i['status']=='downloaded' for i in pictures),
                  unique_files=len({i['sha256'] for i in pictures if i.get('sha256')}),
                  posts_with_images=sum(bool(e.get('images')) for e in state['evidence']),
                  failed=[dict(id=i['id'], reason=i.get('message')) for i in pictures if i['status']=='failed'],
                  historical_snapshots_unchanged=before == frozen_digest(), model_calls=0)
    target=args.root / args.meeting / 'images' / 'collection-summary.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
