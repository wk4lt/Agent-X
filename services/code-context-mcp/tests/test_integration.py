"""Opt-in real Node/CodeGraph + stdio MCP lifecycle; no fake graph or model."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.skipif(os.getenv('CODEGRAPH_INTEGRATION') != '1',
                                reason='set CODEGRAPH_INTEGRATION=1 after npm ci')


def test_durable_build_query_relations_and_project_isolation(tmp_path):
    source = tmp_path / 'sources'
    (source / 'one').mkdir(parents=True)
    (source / 'one' / 'main.py').write_text('def check_ready():\n    return True\n\ndef initialize():\n    return check_ready()\n')
    (source / 'two' / 'src').mkdir(parents=True)
    (source / 'two' / 'src' / 'start.js').write_text('export function waitForSignal() { return true; }\nexport function initialize() { return waitForSignal(); }\n')
    roots = tmp_path / 'roots.json'
    roots.write_text(json.dumps({'source': str(source)}))
    data = tmp_path / 'data'

    def params(writable=True, broken=False):
        return StdioServerParameters(command=sys.executable, args=[
            '-m', 'code_context.server', '--data', str(data), '--roots', str(roots),
            *(['--writable'] if writable else [])], env={**os.environ, **({'CODEGRAPH_NODE': '/missing-node'} if broken else {})})

    async def connect_call(settings, name, args):
        async with stdio_client(settings) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, args)
                assert not result.isError, result.content
                return json.loads(result.content[0].text)

    async def finish(scope, job_id):
        for _ in range(200):
            result = await connect_call(params(False), 'code_status', {**scope, 'job_id': job_id})
            if result['status'] not in {'queued', 'running'}:
                return result
            await asyncio.sleep(0.1)
        raise AssertionError('build did not complete')

    async def run():
        jobs = {}
        for project, path, component, mapping in [('a', 'one', 'subsystem', '.'), ('b', 'two', 'service', 'src')]:
            scope = {'project_id': project, 'baseline_id': 'v1'}
            config = {**scope, 'schema_version': 1,
                      'components': [{'id': component, 'name': component, 'kind': component}],
                      'repositories': [{'id': 'app', 'root_id': 'source', 'relative_path': path, 'revision': 'snapshot',
                                        'mappings': [{'component_id': component, 'path': mapping}]}]}
            # First failure is real missing-runtime failure; retry must recover the same durable job.
            job = await connect_call(params(broken=project == 'a'), 'code_build', {**scope, 'config': config})
            if project == 'a':
                failed = await finish(scope, job['job_id'])
                assert failed['status'] == 'failed'
                assert 'missing-node' in failed['details']['repositories'][0]['error']
                job = await connect_call(params(), 'code_build', {**scope, 'config': config, 'retry': True})
            done = await finish(scope, job['job_id'])
            assert done['status'] == 'ready', done
            jobs[project] = done
            assert done['details']['repositories'][0]['index']['stats']['nodeCount'] >= 3
            duplicate = await connect_call(params(), 'code_build', {**scope, 'config': config})
            assert duplicate['job_id'] == job['job_id']
        scope = {'project_id': 'a', 'baseline_id': 'v1', 'repo_id': 'app'}
        hits = await connect_call(params(False), 'code_search', {**scope, 'query': 'check_ready', 'component_id': 'subsystem'})
        node = next(h['node'] for h in hits['results'] if h['node']['name'] == 'check_ready')
        assert hits['results'][0]['revision'].startswith('snapshot:')
        relations = await connect_call(params(False), 'code_relations', {**scope, 'node_id': node['id']})
        assert any(hit['node']['name'] == 'initialize' for hit in relations['results'])
        (source / 'one' / 'main.py').write_text('mutated after indexing')
        evidence = await connect_call(params(False), 'code_read', {**scope, 'path': 'main.py'})
        assert 'return True' in evidence['excerpt']
        other = await connect_call(params(False), 'code_search', {**scope, 'project_id': 'b', 'query': 'check_ready'})
        assert not other['results']
        javascript = await connect_call(params(False), 'code_search', {**scope, 'project_id': 'b', 'query': 'waitForSignal', 'component_id': 'service'})
        assert any(h['node']['name'] == 'waitForSignal' for h in javascript['results'])
        async with stdio_client(params(False)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert 'code_build' not in {t.name for t in (await session.list_tools()).tools}
                for tool, args in [('code_read', {**scope, 'path': '../roots.json'}),
                                   ('code_read', {**scope, 'path': '.codegraph/codegraph.db'}),
                                   ('code_read', {**scope, 'baseline_id': 'missing', 'path': 'main.py'}),
                                   ('code_status', {'project_id': 'b', 'baseline_id': 'v1', 'job_id': jobs['a']['job_id']})]:
                    assert (await session.call_tool(tool, args)).isError
    asyncio.run(run())
