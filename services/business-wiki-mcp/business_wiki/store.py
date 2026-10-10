"""SQLite source snapshots, lexical RAG, transactional wiki proposals."""
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def tokens(text):
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    return ' '.join(t for w in words for t in ([w] if not re.match(r'[\u3400-\u9fff]', w) else [w[i:i+2] for i in range(max(1, len(w)-1))]))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, revision TEXT, title TEXT, content TEXT, metadata TEXT);
            CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY, revision INTEGER, title TEXT, content TEXT, metadata TEXT, citations TEXT, links TEXT);
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, payload TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, event TEXT, payload TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(kind UNINDEXED, item_id UNINDEXED, chunk UNINDEXED, text, tokenize='unicode61');
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=30000')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def metadata(meta):
        if not isinstance(meta, dict):
            raise ValueError('metadata must be an object')
        for key in ('subsystems', 'features', 'related_subsystems'):
            values = meta.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError(f'{key} must be a string list')
        return meta

    @staticmethod
    def index(db, kind, item_id, title, content):
        db.execute('DELETE FROM search_index WHERE kind=? AND item_id=?', (kind, item_id))
        # Bounded overlapping character chunks retain original text for citations.
        for start in range(0, len(content), 1400):
            chunk = content[start:start+1800]
            db.execute('INSERT INTO search_index VALUES(?,?,?,?)', (kind, item_id, chunk, tokens(title+' '+chunk)))

    def ingest(self, source_id, title, content, metadata):
        if not source_id or not title or not content or len(content) > 2_000_000:
            raise ValueError('nonempty id/title/content required; maximum 2M characters')
        metadata = self.metadata(metadata)
        revision = digest(json.dumps([title, content, metadata], sort_keys=True, ensure_ascii=False))
        with self.db() as db:
            old = db.execute('SELECT revision FROM sources WHERE id=?', (source_id,)).fetchone()
            if old and old['revision'] == revision:
                return {'source_id': source_id, 'revision': revision, 'changed': False}
            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)', (source_id, revision, title, content, json.dumps(metadata)))
            self.index(db, 'source', source_id, title, content)
            db.execute('INSERT INTO audit(event,payload) VALUES(?,?)', ('ingest', json.dumps({'id': source_id, 'revision': revision})))
        return {'source_id': source_id, 'revision': revision, 'changed': True}

    @staticmethod
    def decode(row):
        value = dict(row)
        for key in ('metadata', 'citations', 'links'):
            if key in value:
                value[key] = json.loads(value[key])
        return value

    def read(self, kind, item_id):
        if kind not in ('source', 'wiki'):
            raise ValueError('kind must be source or wiki')
        with self.db() as db:
            row = db.execute(f"SELECT * FROM {'sources' if kind == 'source' else 'pages'} WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise ValueError('item not found')
            result = self.decode(row)
            if kind == 'wiki':
                result['stale'] = any(not (s := db.execute('SELECT revision FROM sources WHERE id=?', (c['source_id'],)).fetchone()) or s['revision'] != c['revision'] for c in result['citations'])
            return result

    def search(self, query, subsystems=None, features=None, kind='all', limit=8):
        if kind not in ('all', 'source', 'wiki') or not 1 <= limit <= 30:
            raise ValueError('invalid kind or limit (1..30)')
        terms = list(dict.fromkeys(tokens(query).split()))[:64]
        if not terms:
            return {'results': [], 'suggestion': 'provide keywords or broaden scope'}
        match = ' OR '.join('"'+t+'"' for t in terms)
        output = []
        with self.db() as db:
            # Scope filtering occurs before LIMIT, preventing global hits starving scoped recall.
            rows = db.execute('SELECT kind,item_id,chunk,bm25(search_index) AS score FROM search_index WHERE search_index MATCH ? ORDER BY score', (match,))
            seen = set()
            for row in rows:
                if kind != 'all' and row['kind'] != ('source' if kind == 'source' else 'wiki'):
                    continue
                key = (row['kind'], row['item_id'])
                if key in seen:
                    continue
                table = 'sources' if row['kind'] == 'source' else 'pages'
                record = self.decode(db.execute(f'SELECT * FROM {table} WHERE id=?', (row['item_id'],)).fetchone())
                meta = record['metadata']
                if subsystems and not set(subsystems).intersection(meta.get('subsystems', []) + meta.get('related_subsystems', [])):
                    continue
                if features and not set(features).intersection(meta.get('features', [])):
                    continue
                seen.add(key)
                output.append({'kind': row['kind'], 'id': row['item_id'], 'title': record['title'], 'revision': record['revision'], 'metadata': meta, 'excerpt': row['chunk'], 'score': row['score']})
                if len(output) == limit:
                    break
        return {'results': output, 'suggestion': None if output else 'broaden features, then related subsystems/COMMON'}

    def propose(self, pages):
        if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
            raise ValueError('1..20 pages required')
        ids = set()
        for p in pages:
            if not isinstance(p, dict) or set(p) != {'id','title','content','metadata','citations','links','expected_revision'}:
                raise ValueError('page fields: id,title,content,metadata,citations,links,expected_revision')
            if not all(isinstance(p[k], str) and p[k] for k in ('id','title','content')) or len(p['content']) > 50000:
                raise ValueError('invalid page text')
            if p['id'] in ids:
                raise ValueError('duplicate page id')
            ids.add(p['id'])
            self.metadata(p['metadata'])
            if not isinstance(p['expected_revision'], int) or isinstance(p['expected_revision'], bool) or p['expected_revision'] < 0:
                raise ValueError('expected_revision must be a nonnegative integer')
            if not isinstance(p['links'], list) or any(not isinstance(x,str) for x in p['links']):
                raise ValueError('links must be a list of page ids')
            if not isinstance(p['citations'], list) or not p['citations']:
                raise ValueError('source citations required')
            for c in p['citations']:
                if not isinstance(c,dict) or set(c) != {'source_id','revision','quote'} or not all(isinstance(v,str) and v for v in c.values()):
                    raise ValueError('citation requires source_id,revision,quote')
        proposal_id = str(uuid.uuid4())
        with self.db() as db:
            db.execute('INSERT INTO proposals VALUES(?,?,?)', (proposal_id,json.dumps(pages), 'pending'))
        return {'proposal_id': proposal_id, 'pages': pages, 'status': 'pending'}

    def apply(self, proposal_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            proposal = db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
            if proposal is None:
                raise ValueError('proposal not found')
            if proposal['status'] == 'applied':
                return {'proposal_id': proposal_id, 'status': 'applied'}
            pages = json.loads(proposal['payload'])
            for p in pages:
                old = db.execute('SELECT revision FROM pages WHERE id=?', (p['id'],)).fetchone()
                if (old['revision'] if old else 0) != p['expected_revision']:
                    raise ValueError('wiki revision conflict; regenerate proposal')
                for c in p['citations']:
                    src = db.execute('SELECT revision,content FROM sources WHERE id=?', (c['source_id'],)).fetchone()
                    if not src or src['revision'] != c['revision'] or c['quote'] not in src['content']:
                        raise ValueError('source revision/quotation mismatch')
            for p in pages:
                db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?,?)', (p['id'],p['expected_revision']+1,p['title'],p['content'],json.dumps(p['metadata']),json.dumps(p['citations']),json.dumps(p['links'])))
                self.index(db,'wiki',p['id'],p['title'],p['content'])
            db.execute("UPDATE proposals SET status='applied' WHERE id=?", (proposal_id,))
            db.execute('INSERT INTO audit(event,payload) VALUES(?,?)', ('apply', proposal['payload']))
        return {'proposal_id': proposal_id, 'status': 'applied', 'page_ids': [p['id'] for p in pages]}

    def lint(self):
        with self.db() as db:
            pages = [self.decode(r) for r in db.execute('SELECT * FROM pages')]
        ids = {p['id'] for p in pages}
        return {'stale_pages': [p['id'] for p in pages if self.read('wiki',p['id'])['stale']], 'broken_links': [{'page':p['id'],'target':x} for p in pages for x in p['links'] if x not in ids]}
