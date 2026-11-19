"""Versioned evidence storage; legacy state is migrated transactionally by Store."""
import copy
import hashlib
import json
import sqlite3
from contextlib import closing


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class EvidenceStorage:
    def migrate(self):
        with self.connect() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version >= 3:
                return
            if conn.execute('SELECT count(*) FROM meetings').fetchone()[0]:
                backup = self.db.with_name('discussions.before-collection-v3.sqlite3')
                if not backup.exists():
                    with closing(sqlite3.connect(backup)) as destination:
                        conn.backup(destination)
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    meeting TEXT, kind TEXT, id TEXT, version TEXT, payload TEXT,
                    source_type TEXT, platform TEXT, title TEXT, text TEXT,
                    PRIMARY KEY(meeting,kind,id,version));
                CREATE TABLE IF NOT EXISTS document_current (
                    meeting TEXT, kind TEXT, id TEXT, version TEXT,
                    PRIMARY KEY(meeting,kind,id));
                CREATE TABLE IF NOT EXISTS snapshot_data (
                    meeting TEXT, id TEXT, payload TEXT, PRIMARY KEY(meeting,id));
                CREATE TABLE IF NOT EXISTS snapshot_documents (
                    meeting TEXT, snapshot TEXT, kind TEXT, id TEXT, version TEXT,
                    PRIMARY KEY(meeting,snapshot,kind,id));
                CREATE TABLE IF NOT EXISTS collection_tasks (
                    meeting TEXT, id TEXT, payload TEXT, PRIMARY KEY(meeting,id));
                CREATE TABLE IF NOT EXISTS classification_cache (
                    key TEXT PRIMARY KEY, label TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS document_filters ON documents(meeting,kind,source_type,platform);
            ''')
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute('SELECT id,state FROM meetings').fetchall():
                state = json.loads(row['state'])
                conn.execute('UPDATE meetings SET state=? WHERE id=?', (encoded(self._pack(conn, state)), row['id']))
            conn.execute('PRAGMA user_version=3')

    def _documents(self, conn, mid, kind, values, current=True):
        refs = []
        for value in values:
            payload = encoded(value)
            version = hashlib.sha256(payload.encode()).hexdigest()
            conn.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?,?,?,?)',
                         (mid, kind, value['id'], version, payload, value.get('source_type', ''),
                          value.get('platform', ''), value.get('title', ''), value.get('text', '')))
            if current:
                conn.execute('INSERT INTO document_current VALUES(?,?,?,?) ON CONFLICT(meeting,kind,id) DO UPDATE SET version=excluded.version WHERE version!=excluded.version',
                             (mid, kind, value['id'], version))
            refs.append((kind, value['id'], version))
        return refs

    def _pack(self, conn, state):
        packed = dict(state)
        packed.pop("_summary_only", None)
        mid = state['id']
        for kind in ('evidence', 'comments'):
            values = packed.pop(kind, [])
            self._documents(conn, mid, kind, values)
            retained = {value['id'] for value in values}
            obsolete = [(mid, kind, row[0]) for row in conn.execute(
                'SELECT id FROM document_current WHERE meeting=? AND kind=?', (mid, kind)) if row[0] not in retained]
            conn.executemany('DELETE FROM document_current WHERE meeting=? AND kind=? AND id=?', obsolete)
            packed[kind + '_count'] = len(values)
        packed['snapshots'] = []
        for snapshot in state.get('snapshots', []):
            sid = snapshot['id']
            if not conn.execute('SELECT 1 FROM snapshot_data WHERE meeting=? AND id=?', (mid, sid)).fetchone():
                item = dict(snapshot)
                refs = self._documents(conn, mid, 'evidence', item.pop('evidence', []), current=False)
                sampled = set((item.get('sentiment') or {}).get('sampling', {}).get('ids', []))
                refs += self._documents(conn, mid, 'comments', [c for c in state.get('comments', []) if c['id'] in sampled], current=False)
                conn.executemany('INSERT OR IGNORE INTO snapshot_documents VALUES(?,?,?,?,?)',
                                 [(mid, sid, *ref) for ref in refs])
                conn.execute('INSERT INTO snapshot_data VALUES(?,?,?)', (mid, sid, encoded(item)))
            packed['snapshots'].append({k: snapshot[k] for k in ('id', 'round', 'created_at', 'summary')})
        packed['_storage_version'] = 3
        return packed

    def _hydrate(self, conn, state, summary=False):
        if not state.get('_storage_version'):
            return state
        mid = state['id']
        state['_summary_only'] = summary
        for kind in ('evidence', 'comments'):
            if summary and kind == 'comments':
                state[kind] = []
                continue
            query = '''SELECT d.payload FROM document_current c JOIN documents d
                USING(meeting,kind,id,version) WHERE c.meeting=? AND c.kind=? ORDER BY c.rowid'''
            if summary:
                query += ' LIMIT 30'
            state[kind] = [json.loads(r[0]) for r in conn.execute(query, (mid, kind))]
        if not summary:
            for i, item in enumerate(state.get('snapshots', [])):
                row = conn.execute('SELECT payload FROM snapshot_data WHERE meeting=? AND id=?', (mid, item['id'])).fetchone()
                snapshot = json.loads(row[0])
                snapshot['evidence'] = [json.loads(r[0]) for r in conn.execute('''SELECT d.payload FROM snapshot_documents s
                    JOIN documents d USING(meeting,kind,id,version) WHERE s.meeting=? AND s.snapshot=? AND s.kind='evidence' ORDER BY s.rowid''', (mid, item['id']))]
                state['snapshots'][i] = snapshot
        return state

    def evidence_page(self, mid, page=0, size=30, query='', source='', platform=''):
        page, size = max(0, int(page)), min(100, max(1, int(size)))
        where = ['c.meeting=?', "c.kind='evidence'"]
        args = [mid]
        for field, value in [('source_type', source), ('platform', platform)]:
            if field == 'source_type' and value == 'image':
                where.append("d.source_type='social_post' AND json_array_length(d.payload, '$.images')>0")
                continue
            if value:
                where.append('d.' + field + '=?')
                args.append(value)
        if query:
            where.append('(instr(d.title,?)>0 OR instr(d.text,?)>0)')
            args.extend([query, query])
        base = ' FROM document_current c JOIN documents d USING(meeting,kind,id,version) WHERE ' + ' AND '.join(where)
        with self.connect() as conn:
            total = conn.execute('SELECT count(*)' + base, args).fetchone()[0]
            items = [json.loads(r[0]) for r in conn.execute('SELECT d.payload' + base + ' ORDER BY c.rowid LIMIT ? OFFSET ?', args + [size, page * size])]
        return dict(items=items, total=total, page=page, size=size)

    def evidence_item(self, mid, eid):
        with self.connect() as conn:
            row = conn.execute('''SELECT d.payload FROM document_current c JOIN documents d USING(meeting,kind,id,version)
                WHERE c.meeting=? AND c.kind='evidence' AND c.id=?''', (mid, eid)).fetchone()
        if not row:
            raise KeyError(eid)
        return json.loads(row[0])

    def collection_tasks(self, mid):
        with self.connect() as conn:
            return [json.loads(r[0]) for r in conn.execute('SELECT payload FROM collection_tasks WHERE meeting=? ORDER BY rowid', (mid,))]

    def save_collection(self, mid, task):
        with self.connect() as conn:
            conn.execute('INSERT INTO collection_tasks VALUES(?,?,?) ON CONFLICT(meeting,id) DO UPDATE SET payload=excluded.payload', (mid, task['id'], encoded(task)))

    def cached_label(self, key, label=None):
        with self.connect() as conn:
            if label is not None:
                conn.execute('INSERT OR REPLACE INTO classification_cache VALUES(?,?)', (key, label))
                return label
            row = conn.execute('SELECT label FROM classification_cache WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
