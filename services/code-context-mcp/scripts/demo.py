#!/usr/bin/env python3
"""Reproducible real-repository walkthrough through BOTH stdio MCP servers.

No LLM credentials needed. This exercises the tools used by project-init;
interactive AI questions are provided by that Skill inside Agent-X.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def client(module, args):
    settings = StdioServerParameters(command=sys.executable, args=['-m', module, *args], env=dict(os.environ))
    async with stdio_client(settings) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async def call(name, arguments):
                result = await session.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError(f'{name}: {result.content}')
                return json.loads(result.content[0].text)
            yield call


async def run(args):
    source, data = Path(args.source).resolve(), Path(args.data).resolve()
    if source == data or data.is_relative_to(source):
        raise ValueError('--data must be outside --source')
    data.mkdir(parents=True, exist_ok=True)
    roots = data / 'roots.json'
    roots.write_text(json.dumps({'source': str(source)}))
    scope = {'project_id': args.project, 'baseline_id': args.baseline}
    common_code = ['--data', str(data / 'code'), '--roots', str(roots), '--project', args.project]
    common_wiki = ['--projects-root', str(data / 'wiki'), '--project', args.project]
    async with client('code_context.server', [*common_code, '--writable']) as code:
        discovery = await code('code_discover', {'root_id': 'source', 'max_files': 1000})
        revision = args.revision or discovery['git_revision'] or 'snapshot'
        async with client('business_wiki.server', [*common_wiki, '--writable']) as wiki:
            draft = await wiki('project_init', {**scope, 'title': args.project})
            if draft['status'] != 'confirmed':
                # Intentionally neutral architecture: one repository component. A real
                # onboarding conversation refines components from user-confirmed evidence.
                patch = {
                    'components': [{'id': 'repository', 'name': args.project, 'kind': 'repository', 'aliases': []}],
                    'repositories': [{'id': 'app', 'root_id': 'source', 'relative_path': '.', 'revision': revision,
                                      'mappings': [{'component_id': 'repository', 'path': '.'}]}],
                    'documents': [{'source_id': 'readme', 'title': args.document, 'component_ids': ['repository'],
                                   'version_status': 'confirmed' if revision != 'snapshot' else 'unknown'}],
                    'features': [{'id': 'demo-feature', 'name': args.feature, 'aliases': [args.query],
                                  'component_ids': ['repository'], 'document_ids': ['readme'], 'code_anchors': []}],
                }
                draft = await wiki('project_propose', {**scope, 'patch': patch, 'expected_revision': draft['revision']})
                draft = await wiki('project_apply', {**scope, 'expected_revision': draft['revision']})
            job = await code('code_build', {**scope, 'config': draft['config'], 'retry': True})
    # All writer connections are now CLOSED. Detached indexing must continue.
    started = time.monotonic()
    async with client('code_context.server', common_code) as code:
        while True:
            status = await code('code_status', {**scope, 'job_id': job['job_id']})
            if status['status'] not in {'queued', 'running'}:
                break
            if time.monotonic() - started > 300:
                raise TimeoutError('build still running; resume with code_status and this job_id: ' + job['job_id'])
            await asyncio.sleep(0.3)
        if status['status'] not in {'ready', 'partial'}:
            raise RuntimeError(json.dumps(status, ensure_ascii=False))
        repo = next(r for r in status['details']['repositories'] if r['repo_id'] == 'app')
        hits = await code('code_search', {**scope, 'repo_id': 'app', 'query': args.query, 'limit': 5})
        if hits['config_hash'] != draft['config_hash']:
            raise RuntimeError('config_hash mismatch')
        if not hits['results']:
            raise RuntimeError('no code results; choose an indexed symbol with --query')
        document = []
        for start in range(1, 4801, 120):
            part = await code('code_read', {**scope, 'repo_id': 'app', 'path': args.document,
                                           'start_line': start, 'end_line': start + 119})
            if part['truncated']:
                raise ValueError('document chunk truncated; import smaller logical sections')
            document.extend(line.split(': ', 1)[1] for line in part['excerpt'].splitlines())
            if part['end_line'] < start + 119:
                break
        else:
            raise ValueError('demo document exceeds 4800 lines')
        node_id = hits['results'][0]['node']['id']
        relations = await code('code_relations', {**scope, 'repo_id': 'app', 'node_id': node_id})
    async with client('business_wiki.server', [*common_wiki, '--writable']) as wiki:
        ingested = await wiki('source_ingest', {**scope, 'source_id': 'readme', 'title': args.document,
                                               'content': '\n'.join(document), 'metadata': {'repo': 'app', 'commit_or_snapshot': repo['revision'], 'path': args.document}})
        route = await wiki('project_route', {**scope, 'query': args.query})
        documents = await wiki('knowledge_search', {**scope, 'query': args.doc_query, 'component_ids': ['repository']})
        if not documents['results']:
            raise RuntimeError('no document results; adjust --doc-query for the selected README')
    evidence = {'scope': scope, 'config_hash': draft['config_hash'], 'discovery': discovery, 'build': status,
                'route': route, 'source': ingested, 'documents': documents, 'code': hits, 'relations': relations}
    (data / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    lines = ['# 真实仓库联合检索验证', '',
             f"执行日期：{datetime.now(timezone.utc).date().isoformat()}（UTC）", '',
             f"- 项目 / 基线：`{args.project}` / `{args.baseline}`",
             f"- 代码版本：`{repo['revision']}`",
             f"- 索引引擎：`{status['details']['engine']}`",
             f"- 构建状态：`{status['status']}`",
             f"- 索引文件 / 节点 / 边：{repo['index']['stats']['fileCount']} / {repo['index']['stats']['nodeCount']} / {repo['index']['stats']['edgeCount']}",
             f"- 配置摘要：`{draft['config_hash']}`", '',
             '本报告由真实 stdio MCP、真实 CodeGraph SDK 与 Wiki 原文检索生成。没有调用语言模型；',
             '用于核验初始化工具链、基线和证据联动，AI 引导对话由 project-init Skill 提供。', '',
             '## 文档证据', '', f"查询：`{args.doc_query}`", '']
    for hit in documents['results'][:2]:
        lines.extend([f"来源：`{hit['id']}`，revision：`{hit['revision']}`", '',
                      '````text', hit['excerpt'][:1800], '````', ''])
    lines.extend(['## 代码证据', '', f"查询：`{args.query}`", ''])
    for hit in hits['results'][:3]:
        node = hit['node']
        lines.extend([f"`{node['filePath']}:{node['startLine']}-{node['endLine']}` — `{node['name']}`", '',
                      '````text', hit['excerpt'][:2000], '````', ''])
    lines.extend(['## 静态调用关系', '',
                  *[f"- `{r['node']['filePath']}:{r['node']['startLine']}` — `{r['node']['name']}`" for r in relations['results'][:10]], '',
                  '边界：此版本没有跨仓调用图和运行轨迹分析。架构示例仅登记一个仓库组件，没有把目录自动确认为业务架构。',
                  '查询使用固定快照；文档入库与代码查询采用同一项目/基线，config_hash 已核对。', ''])
    report = Path(args.report) if args.report else data / 'report.md'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('\n'.join(lines))
    print(json.dumps({'report': str(report), 'evidence': str(data / 'evidence.json'),
                      'job_id': job['job_id'], 'status': status['status'], 'stats': repo['index']['stats']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, help='Local repository; no project code is executed')
    parser.add_argument('--data', required=True, help='Persistent output outside source')
    parser.add_argument('--project', default='demo')
    parser.add_argument('--baseline', default='demo-v1')
    parser.add_argument('--revision', help='Resolved full Git commit; defaults to discovered HEAD or snapshot')
    parser.add_argument('--document', default='README.md')
    parser.add_argument('--query', required=True, help='Code symbol/query')
    parser.add_argument('--doc-query', required=True)
    parser.add_argument('--feature', default='示例特性')
    parser.add_argument('--report')
    asyncio.run(run(parser.parse_args()))
