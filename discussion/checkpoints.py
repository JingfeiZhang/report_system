"""Local atomic checkpoints. Successful model calls are reusable only for identical inputs."""
import hashlib
import json
import os
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def verification_summary(cards):
    latest = {}
    for card in cards:
        if card.get('phase') in ('proposal', 'revision'):
            latest[card.get('agent_id', card['role'])] = card
    claims = [c for card in latest.values() for c in card.get('claims', []) if c.get('action') != 'withdraw']
    pending = sum(c.get('evidence_status', 'unverified') == 'unverified' for c in claims)
    return dict(total=len(claims), checked=len(claims)-pending, pending=pending,
                status='complete' if claims and not pending else 'incomplete')
