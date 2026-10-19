import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from business_wiki.projects import ProjectCatalog


def config_patch():
    return {
        'components': [{'id': 'runtime', 'name': '执行系统', 'kind': 'subsystem', 'aliases': ['Runtime']}],
        'repositories': [{'id': 'app', 'root_id': 'source', 'relative_path': '.', 'revision': 'snapshot',
                          'mappings': [{'component_id': 'runtime', 'path': '.'}]}],
        'documents': [{'source_id': 'design', 'title': '恢复设计', 'component_ids': ['runtime'], 'version_status': 'unknown'}],
        'features': [{'id': 'recovery', 'name': '故障恢复', 'aliases': ['recovery'], 'component_ids': ['runtime'],
                      'document_ids': ['design'], 'code_anchors': [{'repo_id': 'app', 'path': 'main.py', 'symbol': 'recover'}]}],
    }


def test_resume_revision_conflicts_frozen_baseline_and_route(tmp_path):
    catalog = ProjectCatalog(tmp_path, ['a'])
    initial = catalog.init('a', 'v1', 'Project A')
    assert initial['questions']
    draft = catalog.propose('a', 'v1', config_patch(), 0)
    assert catalog.init('a', 'v1', 'ignored')['config'] == draft['config']
    with pytest.raises(ValueError, match='conflict'):
        catalog.propose('a', 'v1', {'title': 'lost update'}, 0)
    with pytest.raises(ValueError, match='conflict'):
        catalog.apply('a', 'v1', 0)
    assert catalog.apply('a', 'v1', 1)['status'] == 'confirmed'
    assert catalog.apply('a', 'v1', 1)['config_hash'] == draft['config_hash']
    with pytest.raises(ValueError, match='frozen'):
        catalog.propose('a', 'v1', {'title': 'overwrite'}, 1)
    route = catalog.route('a', 'v1', 'recovery 失败')
    assert route['repo_ids'] == ['app']
    assert route['features'][0]['code_anchors'][0]['symbol'] == 'recover'
    assert catalog.route('a', 'v1', 'unrecognized')['needs_scope_confirmation']
    with pytest.raises(ValueError, match='not_allowed'):
        catalog.init('b', 'v1', 'B')


def test_scope_isolates_ids_and_preserves_old_evidence(tmp_path):
    catalog = ProjectCatalog(tmp_path)
    for project, baseline, content in [('a', 'v1', 'old recovery'), ('a', 'v2', 'new recovery'), ('b', 'v1', 'other recovery')]:
        catalog.init(project, baseline, project)
        catalog.propose(project, baseline, config_patch(), 0)
        store, _ = catalog.store(project, baseline)
        store.ingest('design', 'Recovery', content, {})
    first, _ = catalog.store('a', 'v1')
    rev = first.read('source', 'design')['revision']
    first.ingest('design', 'Recovery', 'changed recovery', {})
    assert first.read('source', 'design', rev)['content'] == 'old recovery'
    assert first.read('source', 'design')['content'] == 'changed recovery'
    second, _ = catalog.store('a', 'v2')
    assert second.search('recovery')['results'][0]['excerpt'] == 'new recovery'
    with pytest.raises(ValueError, match='revision not found'):
        second.read('source', 'design', rev)
    assert not first.search('other')['results']
    with pytest.raises(ValueError, match='not_found'):
        catalog.store('a', 'missing')


def test_invalid_architecture_and_unbound_references_rejected(tmp_path):
    catalog = ProjectCatalog(tmp_path)
    catalog.init('a', 'v1', 'A')
    patch = config_patch()
    patch['relations'] = [{'from': 'runtime', 'to': 'runtime', 'type': 'part_of',
                           'status': 'confirmed', 'provenance': {'kind': 'user_confirmation'}}]
    with pytest.raises(ValueError, match='cycle'):
        catalog.propose('a', 'v1', patch, 0)
    patch.pop('relations')
    patch['repositories'][0]['relative_path'] = '../outside'
    with pytest.raises(ValueError, match='relative_path'):
        catalog.propose('a', 'v1', patch, 0)
    patch = config_patch()
    patch['features'][0]['document_ids'] = ['undeclared']
    with pytest.raises(ValueError, match='feature_document'):
        catalog.propose('a', 'v1', patch, 0)


def test_strict_project_stdio_no_scope_fallback(tmp_path):
    async def run():
        params = StdioServerParameters(command=sys.executable, args=[
            '-m', 'business_wiki.server', '--projects-root', str(tmp_path), '--project', 'a', '--writable'],
            env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, result.content
                    return json.loads(result.content[0].text)
                scope = {'project_id': 'a', 'baseline_id': 'v1'}
                await call('project_init', {**scope, 'title': 'A'})
                await call('project_propose', {**scope, 'patch': config_patch(), 'expected_revision': 0})
                result = await call('source_ingest', {**scope, 'source_id': 'design', 'title': '恢复设计',
                                                     'content': '恢复需要就绪', 'metadata': {'version_status': 'confirmed'}})
                source = await call('knowledge_read', {**scope, 'kind': 'source', 'item_id': 'design'})
                assert source['metadata']['version_status'] == 'unknown'
                hits = await call('knowledge_search', {**scope, 'query': '恢复', 'component_ids': ['runtime']})
                assert hits['results'][0]['project_id'] == 'a'
                for name, args in [
                    ('knowledge_search', {'query': '恢复'}),
                    ('knowledge_read', {**scope, 'project_id': 'b', 'kind': 'source', 'item_id': 'design'}),
                    ('source_ingest', {**scope, 'source_id': 'unregistered', 'title': 'X', 'content': 'X', 'metadata': {}}),
                ]:
                    assert (await session.call_tool(name, args)).isError
    asyncio.run(run())


def test_derived_wiki_preserves_unknown_version_and_old_page_revision(tmp_path):
    from business_wiki.store import Store
    store = Store(tmp_path / 'wiki.db')
    source = store.ingest('design', 'Startup', 'Ready before start.', {'version_status': 'unknown'})
    page = dict(id='startup', title='Startup', content='Ready before start.', metadata={},
                citations=[{'source_id': 'design', 'revision': source['revision'], 'quote': 'Ready before start.'}],
                links=[], expected_revision=0)
    store.apply(store.propose([page])['proposal_id'])
    assert store.read('wiki', 'startup')['evidence_version_status'] == 'unknown'
    assert store.search('Startup', kind='wiki')['results'][0]['evidence_version_status'] == 'unknown'
    page.update(content='Updated summary.', expected_revision=1)
    store.apply(store.propose([page])['proposal_id'])
    assert store.read('wiki', 'startup', '1')['content'] == 'Ready before start.'
    assert store.read('wiki', 'startup')['content'] == 'Updated summary.'
