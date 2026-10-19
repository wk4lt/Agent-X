"""Project onboarding catalog. One document database per project/baseline scope."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3

ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('invalid_identifier')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def string_list(value):
    return isinstance(value, list) and all(isinstance(v, str) and 0 < len(v) <= 200 for v in value)


def validate_config(config, project_id, baseline_id):
    if not isinstance(config, dict) or len(json.dumps(config)) > 500_000:
        raise ValueError('invalid_or_oversized_config')
    allowed = {'schema_version', 'project_id', 'baseline_id', 'title', 'components',
               'repositories', 'features', 'relations', 'documents', 'notes'}
    if set(config) - allowed:
        raise ValueError('unknown_config_fields')
    if config.get('schema_version') != 1 or config.get('project_id') != project_id or config.get('baseline_id') != baseline_id:
        raise ValueError('scope_or_schema_mismatch')
    if not isinstance(config.get('title'), str) or not 1 <= len(config['title']) <= 200:
        raise ValueError('title_required')
    for field in ('components', 'repositories', 'features', 'relations', 'documents'):
        if not isinstance(config.get(field, []), list) or len(config.get(field, [])) > 1000:
            raise ValueError(f'invalid_{field}')
    components, repos, features = set(), set(), set()
    for field, seen in [('components', components), ('repositories', repos), ('features', features)]:
        for item in config.get(field, []):
            if not isinstance(item, dict):
                raise ValueError(f'invalid_{field}_item')
            ident = identifier(item.get('id'))
            if ident in seen:
                raise ValueError(f'duplicate_{field}_id')
            seen.add(ident)
            if field != 'repositories':
                if not isinstance(item.get('name'), str) or not 1 <= len(item['name']) <= 200:
                    raise ValueError('component_or_feature_name_required')
                if not string_list(item.get('aliases', [])):
                    raise ValueError('aliases_must_be_strings')
    for component in config.get('components', []):
        if not isinstance(component.get('kind', 'module'), str):
            raise ValueError('component_kind_must_be_string')
    document_ids = {d.get('source_id') for d in config.get('documents', []) if isinstance(d, dict)}
    for feature in config.get('features', []):
        if not string_list(feature.get('component_ids', [])) or set(feature.get('component_ids', [])) - components:
            raise ValueError('unknown_feature_component')
        if not string_list(feature.get('document_ids', [])) or set(feature.get('document_ids', [])) - document_ids:
            raise ValueError('unknown_feature_document')
        for anchor in feature.get('code_anchors', []):
            if anchor.get('repo_id') not in repos or not isinstance(anchor.get('symbol', ''), str):
                raise ValueError('invalid_code_anchor')
            path = anchor.get('path', '')
            if not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path:
                raise ValueError('invalid_code_anchor_path')
    for repo in config.get('repositories', []):
        identifier(repo.get('root_id'))
        revision = repo.get('revision')
        if revision != 'snapshot' and (not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40,64}', revision)):
            raise ValueError('repository_revision_must_be_commit_or_snapshot')
        paths = [repo.get('relative_path')]
        if not isinstance(repo.get('mappings', []), list):
            raise ValueError('invalid_repository_mappings')
        for mapping in repo.get('mappings', []):
            if mapping.get('component_id') not in components:
                raise ValueError('unknown_repository_component')
            paths.append(mapping.get('path'))
        for value in paths:
            if (not isinstance(value, str) or not value or PurePosixPath(value).is_absolute()
                    or '..' in PurePosixPath(value).parts or '\\' in value):
                raise ValueError('invalid_relative_path')
    parents = {}
    for relation in config.get('relations', []):
        if relation.get('from') not in components or relation.get('to') not in components:
            raise ValueError('unknown_relation_component')
        if not isinstance(relation.get('type'), str) or not relation['type']:
            raise ValueError('relation_type_required')
        if relation.get('status') not in {'proposed', 'confirmed', 'rejected'}:
            raise ValueError('relation_status_required')
        if relation['type'] == 'part_of' and relation['status'] == 'confirmed':
            parents.setdefault(relation['from'], []).append(relation['to'])
        if relation['status'] == 'confirmed' and not relation.get('provenance'):
            raise ValueError('confirmed_relation_requires_provenance')
    def visit(node, stack):
        if node in stack:
            raise ValueError('part_of_cycle')
        for parent in parents.get(node, []):
            visit(parent, stack | {node})
    for node in parents:
        visit(node, set())
    for doc in config.get('documents', []):
        identifier(doc.get('source_id'))
        if doc.get('version_status') not in {'confirmed', 'unknown'}:
            raise ValueError('document_version_status_required')
        if not string_list(doc.get('component_ids', [])) or set(doc.get('component_ids', [])) - components:
            raise ValueError('unknown_document_component')
    return config


def questions(config):
    output = []
    if not config.get('repositories'):
        output.append({'field': 'repositories', 'question': '代码来自哪些已登记资料根？使用发现报告中的 commit，或固定快照。'})
    if not config.get('components'):
        output.append({'field': 'components', 'question': '项目有哪些组件？请核对自动发现的目录与架构文档，目录名不等于业务组件。'})
    if not config.get('documents'):
        output.append({'field': 'documents', 'question': '哪些文档是设计依据？如果没有设计资料，可以先只构建代码。'})
    unknown = [d['source_id'] for d in config.get('documents', []) if d['version_status'] == 'unknown']
    if unknown:
        output.append({'field': 'documents.version_status', 'question': '这些文档是否适用于本基线？不能确认时保留 unknown。', 'sources': unknown})
    return output


class ProjectCatalog:
    def __init__(self, root, allowed_projects=()):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_projects = set(allowed_projects)
        for value in self.allowed_projects:
            identifier(value)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS projects (
                project TEXT, baseline TEXT, revision INTEGER, status TEXT,
                config TEXT, PRIMARY KEY(project,baseline))''')
            db.execute('''CREATE TABLE IF NOT EXISTS project_audit (
                id INTEGER PRIMARY KEY, project TEXT, baseline TEXT, revision INTEGER,
                action TEXT, payload TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP)''')

    @contextmanager
    def db(self):
        with sqlite3.connect(self.root / 'catalog.sqlite3', timeout=30) as db:
            db.row_factory = sqlite3.Row
            yield db

    def check_scope(self, project, baseline):
        identifier(project)
        identifier(baseline)
        if self.allowed_projects and project not in self.allowed_projects:
            raise ValueError('project_not_allowed')

    @staticmethod
    def view(row):
        config = json.loads(row['config'])
        return {'project_id': row['project'], 'baseline_id': row['baseline'], 'revision': row['revision'],
                'status': row['status'], 'config': config, 'config_hash': fingerprint(config),
                'questions': questions(config), 'note': 'Confirmed config is not proof of index readiness; inspect code_status.'}

    def get(self, project, baseline):
        self.check_scope(project, baseline)
        with self.db() as db:
            row = db.execute('SELECT * FROM projects WHERE project=? AND baseline=?', (project, baseline)).fetchone()
            if row is None:
                raise ValueError('project_baseline_not_found')
            return self.view(row)

    def init(self, project, baseline, title):
        self.check_scope(project, baseline)
        config = {'schema_version': 1, 'project_id': project, 'baseline_id': baseline, 'title': title,
                  'components': [], 'repositories': [], 'features': [], 'relations': [], 'documents': []}
        validate_config(config, project, baseline)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO projects VALUES(?,?,0,?,?)',
                       (project, baseline, 'draft', json.dumps(config)))
        return self.get(project, baseline)

    def propose(self, project, baseline, patch, expected_revision):
        self.check_scope(project, baseline)
        if not isinstance(patch, dict) or not patch or isinstance(expected_revision, bool):
            raise ValueError('invalid_patch_or_revision')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM projects WHERE project=? AND baseline=?', (project, baseline)).fetchone()
            if row is None or row['revision'] != expected_revision:
                raise ValueError('project_revision_conflict')
            if row['status'] == 'confirmed':
                raise ValueError('baseline_frozen: create a new baseline')
            old = json.loads(row['config'])
            config = validate_config({**old, **patch}, project, baseline)
            revision = expected_revision + 1
            db.execute('UPDATE projects SET config=?,revision=? WHERE project=? AND baseline=?',
                       (json.dumps(config), revision, project, baseline))
            db.execute('INSERT INTO project_audit(project,baseline,revision,action,payload) VALUES(?,?,?,?,?)',
                       (project, baseline, revision, 'propose', json.dumps({'before': old, 'after': config})))
        return self.get(project, baseline)

    def apply(self, project, baseline, expected_revision):
        self.check_scope(project, baseline)
        if isinstance(expected_revision, bool):
            raise ValueError('invalid_revision')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM projects WHERE project=? AND baseline=?', (project, baseline)).fetchone()
            if row is None or row['revision'] != expected_revision:
                raise ValueError('project_revision_conflict')
            config = json.loads(row['config'])
            if not config.get('components') or not config.get('repositories'):
                raise ValueError('components_and_repositories_required')
            validate_config(config, project, baseline)
            if row['status'] != 'confirmed':
                db.execute("UPDATE projects SET status='confirmed' WHERE project=? AND baseline=?", (project, baseline))
                db.execute('INSERT INTO project_audit(project,baseline,revision,action,payload) VALUES(?,?,?,?,?)',
                           (project, baseline, expected_revision, 'apply', row['config']))
        return self.get(project, baseline)

    def store(self, project, baseline):
        from .store import Store
        config = self.get(project, baseline)
        key = fingerprint([project, baseline])
        return Store(self.root / 'scopes' / key / 'wiki.sqlite3'), config

    def route(self, project, baseline, query):
        config = self.get(project, baseline)['config']
        text = query.casefold()
        components = []
        for item in config['components']:
            names = [item['id'], item['name'], *item.get('aliases', [])]
            matched = [name for name in names if name.casefold() in text]
            if matched:
                components.append(item['id'])
        feature_ids = []
        for feature in config['features']:
            if any(name.casefold() in text for name in [feature['name'], *feature.get('aliases', [])]):
                feature_ids.append(feature['id'])
                components.extend(feature.get('component_ids', []))
        components = sorted(set(components))
        repos = [r['id'] for r in config['repositories']
                 if any(m['component_id'] in components for m in r.get('mappings', []))]
        return {'project_id': project, 'baseline_id': baseline, 'component_ids': components,
                'feature_ids': feature_ids, 'repo_ids': repos,
                'features': [f for f in config['features'] if f['id'] in feature_ids], 'needs_scope_confirmation': not components,
                'relations': [r for r in config['relations'] if r['status'] == 'confirmed'
                              and (r['from'] in components or r['to'] in components)],
                'note': 'Alias routing is a hint; expand related components using evidence, never cross project/baseline.'}
