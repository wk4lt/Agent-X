"""Durable build jobs and immutable code snapshots. Linux/POSIX deployment."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager

ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')
SKIP = {'.git', '.codegraph', '.data', '.venv', 'node_modules', '__pycache__', 'dist', 'build'}
ENGINE_VERSION = 'codegraph-1.6.2-adapter-1'


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('invalid_identifier')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value:
        raise ValueError('invalid_relative_path')
    return path


class CodeService:
    def __init__(self, data, roots, allowed_projects=()):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.roots = {identifier(k): Path(v).resolve(strict=True) for k, v in roots.items()}
        self.allowed_projects = set(allowed_projects)
        for value in self.allowed_projects:
            identifier(value)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project TEXT, baseline TEXT, config_hash TEXT,
                payload TEXT, status TEXT, result TEXT, created REAL)''')

    @contextmanager
    def db(self):
        with sqlite3.connect(self.data / 'builds.sqlite3', timeout=30) as db:
            db.row_factory = sqlite3.Row
            yield db

    def scope(self, project_id, baseline_id):
        identifier(project_id)
        identifier(baseline_id)
        if self.allowed_projects and project_id not in self.allowed_projects:
            raise ValueError('project_not_allowed')

    def source(self, root_id, path):
        base = self.roots.get(root_id)
        if base is None:
            raise ValueError('unknown_root')
        rel = relative(path)
        target = (base / rel).resolve(strict=True)
        if not target.is_relative_to(base) or not target.is_dir():
            raise ValueError('source_outside_root')
        # Reject symlinks even when they happen to point back inside the root.
        cursor = base
        for part in rel.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError('source_symlink')
        if target == self.data or self.data.is_relative_to(target):
            raise ValueError('data_directory_must_be_outside_source')
        return target

    def discover(self, root_id, path='.', max_files=1000):
        if not 1 <= max_files <= 5000:
            raise ValueError('max_files must be 1..5000')
        source = self.source(root_id, path)
        files, docs, suffixes = [], [], {}
        incomplete = False
        for directory, names, filenames in os.walk(source, followlinks=False):
            names[:] = sorted(n for n in names if n not in SKIP and not n.startswith('.')
                              and not (Path(directory) / n).is_symlink())
            for name in sorted(filenames):
                entry = Path(directory) / name
                if entry.is_symlink() or not entry.is_file() or name.startswith('.'):
                    continue
                if len(files) >= max_files:
                    incomplete = True
                    break
                rel = entry.relative_to(source).as_posix()
                files.append(rel)
                suffixes[entry.suffix or '(none)'] = suffixes.get(entry.suffix or '(none)', 0) + 1
                if entry.suffix.lower() in {'.md', '.txt', '.rst'} and len(docs) < 12:
                    docs.append({'path': rel, 'preview': entry.read_bytes()[:4000].decode('utf-8', 'replace')})
            if incomplete:
                break
        revision = None
        try:
            top = self.git(source, 'rev-parse', '--show-toplevel').strip()
            if Path(top).resolve() == source:
                revision = self.git(source, 'rev-parse', 'HEAD').strip()
        except (subprocess.SubprocessError, OSError):
            pass
        return {'root_id': root_id, 'relative_path': path, 'git_revision': revision,
                'files': files, 'extensions': suffixes, 'documents': docs,
                'candidate_directories': sorted({p.split('/')[0] for p in files if '/' in p}),
                'discovery_incomplete': incomplete,
                'architecture_status': 'unconfirmed',
                'note': 'Directory names are observations, not confirmed components.'}

    @staticmethod
    def git(path, *args):
        return subprocess.check_output(['git', '-C', str(path), *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=30)

    def validate(self, project_id, baseline_id, config):
        self.scope(project_id, baseline_id)
        if (config.get('schema_version') != 1 or config.get('project_id') != project_id
                or config.get('baseline_id') != baseline_id):
            raise ValueError('scope_or_schema_mismatch')
        repos = config.get('repositories', [])
        if not isinstance(repos, list) or not 1 <= len(repos) <= 1000:
            raise ValueError('1..1000 repositories required')
        seen = set()
        for repo in repos:
            rid = identifier(repo['id'])
            if rid in seen:
                raise ValueError('duplicate_repository')
            seen.add(rid)
            self.source(repo['root_id'], repo['relative_path'])
            rev = repo.get('revision')
            if rev != 'snapshot' and (not isinstance(rev, str) or not re.fullmatch(r'[a-f0-9]{40,64}', rev)):
                raise ValueError('revision must be a resolved commit or snapshot')

    def build(self, project_id, baseline_id, config, retry=False):
        self.validate(project_id, baseline_id, config)
        config_hash = fingerprint(config)
        job_id = fingerprint([config_hash, ENGINE_VERSION])[:32]
        payload = json.dumps(config)
        # Retrying only terminal jobs avoids spawning duplicate workers for one build.
        self.status(project_id, baseline_id, job_id, missing_ok=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row and (not retry or row['status'] not in {'failed', 'interrupted'}):
                return self.view(row)
            # A published baseline cannot silently switch to another config.
            other = db.execute("SELECT id FROM jobs WHERE project=? AND baseline=? AND config_hash<>? AND status IN ('ready','partial','running','queued')",
                               (project_id, baseline_id, config_hash)).fetchone()
            if other:
                raise ValueError('baseline_config_conflict: create a new baseline')
            db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                       (job_id, project_id, baseline_id, config_hash, payload, 'queued', '{}', time.time()))
        log = self.data / f'{job_id}.log'
        with log.open('ab') as output:
            subprocess.Popen([sys.executable, '-m', 'code_context.worker', '--data', str(self.data),
                              '--roots', json.dumps({k: str(v) for k, v in self.roots.items()}),
                              '--job', job_id], stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                             start_new_session=True, close_fds=True)
        return self.status(project_id, baseline_id, job_id)

    @staticmethod
    def view(row):
        return {'job_id': row['id'], 'project_id': row['project'], 'baseline_id': row['baseline'],
                'config_hash': row['config_hash'], 'status': row['status'], 'details': json.loads(row['result'])}

    def status(self, project_id, baseline_id, job_id, missing_ok=False):
        self.scope(project_id, baseline_id)
        identifier(job_id)
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND project=? AND baseline=?',
                             (job_id, project_id, baseline_id)).fetchone()
            if row is None:
                if missing_ok:
                    return None
                raise ValueError('job_not_found')
            if row['status'] == 'running' or (row['status'] == 'queued' and time.time() - row['created'] > 30):
                with (self.data / f'{job_id}.lock').open('a') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        db.execute("UPDATE jobs SET status='interrupted' WHERE id=? AND status IN ('queued','running')", (job_id,))
                        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            return self.view(row)

    def update(self, job_id, status, details):
        with self.db() as db:
            db.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status, json.dumps(details), job_id))

    def worker(self, job_id):
        identifier(job_id)
        with (self.data / f'{job_id}.lock').open('a') as lock:
            # A retry may arrive just before the previous worker releases its lock.
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Limit local index builds to one worker; query processes use published snapshots.
            with (self.data / 'build.lock').open('a') as global_lock:
                fcntl.flock(global_lock, fcntl.LOCK_EX)
                with self.db() as db:
                    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row['status'] not in {'queued', 'interrupted'}:
                    return
                config = json.loads(row['payload'])
                details = {'repositories': [], 'engine': ENGINE_VERSION,
                           'capabilities': {'cross_repo_calls': False, 'immutable_snapshots': True}}
                self.update(job_id, 'running', details)
                try:
                    for repo in config['repositories']:
                        result = {'repo_id': repo['id'], 'status': 'running', 'stage': 'snapshot', 'started_at': time.time()}
                        details['repositories'].append(result)
                        self.update(job_id, 'running', details)
                        try:
                            target = self.data / 'snapshots' / job_id / repo['id']
                            if target.exists():
                                shutil.rmtree(target)
                            target.mkdir(parents=True)
                            result['revision'] = self.snapshot(repo, target)
                            result['stage'] = 'index'
                            self.update(job_id, 'running', details)
                            result['index'] = self.node({'action': 'build', 'path': str(target)}, timeout=900)
                            stats = result['index']['stats']
                            if stats['nodeCount'] == 0:
                                raise ValueError('no_indexed_symbols: check CodeGraph language support')
                            result['status'] = 'partial' if result['index']['result'].get('filesErrored', 0) else 'ready'
                        except Exception as exc:
                            result.update(status='failed', error_type=type(exc).__name__, error=str(exc)[-4000:])
                        result['finished_at'] = time.time()
                        self.update(job_id, 'running', details)
                    statuses = [r['status'] for r in details['repositories']]
                    final = 'ready' if all(s == 'ready' for s in statuses) else (
                        'partial' if any(s in {'ready', 'partial'} for s in statuses) else 'failed')
                    self.update(job_id, final, details)
                except BaseException as exc:
                    details['error'] = str(exc)[-4000:]
                    self.update(job_id, 'failed', details)
                    raise

    def snapshot(self, repo, target):
        source = self.source(repo['root_id'], repo['relative_path'])
        max_files, max_bytes, max_file = 100_000, 2 * 1024**3, 20 * 1024**2
        total = count = 0
        hashes = []

        def write(name, stream, size):
            nonlocal count, total
            rel = relative(name)
            if any(p in SKIP for p in rel.parts):
                return
            if size > max_file or count >= max_files or total + size > max_bytes:
                raise ValueError('snapshot_limit_exceeded')
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob = stream.read(max_file + 1)
            if len(blob) != size:
                raise ValueError('source_changed_during_snapshot')
            dest.write_bytes(blob)
            hashes.append([rel.as_posix(), hashlib.sha256(blob).hexdigest()])
            total += size
            count += 1

        if repo['revision'] != 'snapshot':
            if Path(self.git(source, 'rev-parse', '--show-toplevel').strip()).resolve() != source:
                raise ValueError('git_source_must_be_repository_root')
            revision = self.git(source, 'rev-parse', '--verify', repo['revision'] + '^{commit}').strip()
            # Stream the archive so size limits stop a large export early. Never extract links/devices.
            with tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen(['git', '-C', str(source), 'archive', '--format=tar', revision],
                                        stdout=subprocess.PIPE, stderr=errors)
                try:
                    with tarfile.open(fileobj=proc.stdout, mode='r|') as tar:
                        for item in tar:
                            if item.isfile():
                                with tar.extractfile(item) as stream:
                                    write(item.name, stream, item.size)
                    if proc.wait(timeout=120):
                        raise ValueError('git_archive_failed')
                finally:
                    proc.stdout.close()
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
            return revision
        for directory, names, files in os.walk(source, followlinks=False):
            names[:] = sorted(n for n in names if n not in SKIP and not n.startswith('.')
                              and not (Path(directory) / n).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file() or name.startswith('.'):
                    continue
                with path.open('rb') as stream:
                    write(path.relative_to(source).as_posix(), stream, path.stat().st_size)
        return 'snapshot:' + fingerprint(sorted(hashes))

    @staticmethod
    def node(request, timeout=45):
        proc = subprocess.run([os.getenv('CODEGRAPH_NODE', 'node'), str(Path(__file__).with_name('bridge.mjs'))],
                              input=json.dumps(request), text=True, capture_output=True, timeout=timeout,
                              env={**os.environ, 'CODEGRAPH_TELEMETRY': '0', 'CODEGRAPH_NO_UPDATE_CHECK': '1'})
        if proc.returncode:
            raise ValueError('codegraph_failed: ' + proc.stderr[-4000:])
        return json.loads(proc.stdout)

    def published(self, project_id, baseline_id, repo_id):
        self.scope(project_id, baseline_id)
        identifier(repo_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE project=? AND baseline=? AND status IN ('ready','partial') ORDER BY created DESC LIMIT 1",
                             (project_id, baseline_id)).fetchone()
        if row is None:
            raise ValueError('baseline_not_ready')
        result = next((r for r in json.loads(row['result'])['repositories'] if r['repo_id'] == repo_id), None)
        if not result or result['status'] not in {'ready', 'partial'}:
            raise ValueError('repository_not_ready')
        return row, result, self.data / 'snapshots' / row['id'] / repo_id

    def search(self, project_id, baseline_id, repo_id, query, limit=8, component_id=None):
        if not query.strip() or len(query) > 2000 or not 1 <= limit <= 30:
            raise ValueError('invalid_query_or_limit')
        row, info, target = self.published(project_id, baseline_id, repo_id)
        config = json.loads(row['payload'])
        repo = next(r for r in config['repositories'] if r['id'] == repo_id)
        patterns = []
        if component_id:
            mapping = [m for m in repo.get('mappings', []) if m['component_id'] == component_id]
            if not mapping:
                return {'results': [], 'reason': 'component_not_mapped_to_repository'}
            for item in mapping:
                path = relative(item['path']).as_posix()
                patterns.append('**' if path == '.' else (path if (target / path).is_file() else path.rstrip('/') + '/**'))
        result = self.node({'action': 'search', 'path': str(target), 'query': query, 'limit': limit, 'patterns': patterns})
        for hit in result['results']:
            hit.update(kind='code', project_id=project_id, baseline_id=baseline_id,
                       repo_id=repo_id, revision=info['revision'], index_revision=row['id'])
        result.update(config_hash=row['config_hash'], coverage_status=info['status'])
        return result

    def read(self, project_id, baseline_id, repo_id, path, start_line=1, end_line=120):
        row, info, target = self.published(project_id, baseline_id, repo_id)
        rel = relative(path)
        if '.codegraph' in rel.parts or any(p.startswith('.') for p in rel.parts):
            raise ValueError('internal_path')
        file = (target / rel).resolve(strict=True)
        if not file.is_relative_to(target) or not file.is_file() or file.is_symlink():
            raise ValueError('invalid_code_file')
        if start_line < 1 or end_line < start_line or end_line - start_line > 300:
            raise ValueError('line_range_limit_301')
        if file.stat().st_size > 20 * 1024**2:
            raise ValueError('file_too_large')
        lines = file.read_text(encoding='utf-8', errors='replace').splitlines()
        text = '\n'.join(f'{i}: {line}' for i, line in enumerate(lines[start_line-1:end_line], start_line))
        return {'kind': 'code', 'project_id': project_id, 'baseline_id': baseline_id, 'repo_id': repo_id,
                'revision': info['revision'], 'index_revision': row['id'], 'path': rel.as_posix(),
                'start_line': start_line, 'end_line': min(end_line, len(lines)), 'excerpt': text[:24000],
                'truncated': len(text) > 24000}

    def relations(self, project_id, baseline_id, repo_id, node_id, direction='callers', depth=1):
        if direction not in {'callers', 'callees'} or not 1 <= depth <= 3:
            raise ValueError('invalid_direction_or_depth')
        row, info, target = self.published(project_id, baseline_id, repo_id)
        return {**self.node({'action': 'relations', 'path': str(target), 'node_id': node_id,
                            'direction': direction, 'depth': depth, 'limit': 50}),
                'project_id': project_id, 'baseline_id': baseline_id, 'repo_id': repo_id,
                'revision': info['revision'], 'index_revision': row['id'], 'cross_repo_calls': False}
