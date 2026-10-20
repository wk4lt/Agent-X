"""Maintenance allowlisting and opt-in actual SDK v2 Host -> SDK v1 service interoperability."""
import json
import os
from pathlib import Path

import pytest
from contracts.models import CreateRunRequest, RiskLevel, ToolCall, ToolSpec
from harness.app import create_app
from harness.config import ConfigurationError, HarnessSettings
from harness.mcp_tools import McpServerConfig, McpToolRegistry, load_mcp_servers
from harness.provider import ProviderDelta, ScriptedMockProvider
from harness.skills import discover_skills
from harness.tools import ToolCatalog, ToolPolicy


async def unused(_):
    raise AssertionError('should not execute')


def test_maintenance_allowlist_does_not_enable_other_writes_or_commands():
    catalog = ToolCatalog()
    for name, source, risk, approval in [
        ('read', 'mcp:reader', RiskLevel.read_only, False),
        ('maintain', 'mcp:writer', RiskLevel.workspace_write, False),
        ('other', 'mcp:other', RiskLevel.workspace_write, False),
        ('command', 'mcp:writer', RiskLevel.command_exec, False),
        ('external', 'mcp:writer', RiskLevel.external_side_effect, False),
        ('approval', 'mcp:writer', RiskLevel.workspace_write, True),
    ]:
        catalog.register(ToolSpec(name=name, source=source, risk=risk, description=name,
                                  input_schema={'type': 'object'}, requires_approval=approval), unused)
    policy = ToolPolicy(maintenance_sources=frozenset({'mcp:writer'}))
    assert {t.spec.name for t in policy.filter(catalog)} == {'read', 'maintain', 'approval'}
    assert policy.validate(catalog.get('maintain'), ToolCall(name='maintain')) is None
    for name in ['other', 'command', 'external', 'approval']:
        assert policy.validate(catalog.get(name), ToolCall(name=name)).error_code == 'approval_required'
    assert ToolPolicy().validate(catalog.get('maintain'), ToolCall(name='maintain')).error_code == 'approval_required'


def test_new_skills_and_config_are_discoverable(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    catalog = discover_skills(root)
    assert not catalog.diagnostics
    assert {'project-init', 'project-qa'} <= set(catalog.records)
    assert 'project_maintenance' not in catalog.records['project-qa'].summary.tool_groups
    servers = load_mcp_servers(root / 'services/code-context-mcp/agent-x.example.yaml')
    assert len(servers) == 4
    monkeypatch.setenv('MCP_MAINTENANCE_SERVERS', 'project_wiki_writer,project_code_writer')
    assert HarnessSettings.from_environment().mcp_maintenance_servers == ('project_wiki_writer', 'project_code_writer')
    monkeypatch.setenv('MCP_MAINTENANCE_SERVERS', '*')
    with pytest.raises(ConfigurationError, match='explicit MCP server names'):
        HarnessSettings.from_environment()


@pytest.mark.skipif(not os.getenv('PROJECT_SERVICE_PYTHON'), reason='requires independently installed SDK v1 service venv')
async def test_real_harness_can_initialize_and_read_project(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('SKILL_PROJECT_ROOT', str(root))
    monkeypatch.setenv('MCP_MAINTENANCE_SERVERS', 'wiki_writer')
    monkeypatch.setenv('MCP_CONFIG_PATH', str(tmp_path / 'absent.yaml'))
    monkeypatch.setenv('WORKSPACE_ROOT', str(tmp_path / 'workspaces'))
    monkeypatch.setenv('LOG_ROOT', str(tmp_path / 'logs'))
    settings = HarnessSettings.from_environment()
    servers = tuple(McpServerConfig(
        name=name, transport='stdio', tool_groups=(group,),
        command=os.environ['PROJECT_SERVICE_PYTHON'],
        args=('-m', 'business_wiki.server', '--projects-root', str(tmp_path / 'wiki'), *extra),
        allowed_tools=frozenset(names), risk=risk,
    ) for name, group, extra, names, risk in [
        ('wiki_writer', 'project_maintenance', ('--writable',), ['project_init'], RiskLevel.workspace_write),
        ('wiki_reader', 'project_read', (), ['project_get'], RiskLevel.read_only),
    ])
    scope = {'project_id': 'demo', 'baseline_id': 'v1'}
    provider = ScriptedMockProvider([
        [ProviderDelta(tool_calls=[ToolCall(name='mcp__wiki_writer__project_init', arguments={**scope, 'title': 'Demo'})], finish_reason='tool_calls')],
        [ProviderDelta(tool_calls=[ToolCall(name='mcp__wiki_reader__project_get', arguments=scope)], finish_reason='tool_calls')],
        [ProviderDelta(text='draft initialized', finish_reason='stop')],
    ])
    app = create_app(provider=provider, settings=settings)
    executor = app.state.executor
    executor.mcp_registry = McpToolRegistry(servers)
    request = CreateRunRequest(task_id='task_project', idempotency_key='init', input='初始化 demo', selected_skills=['project-init'])
    run, _ = await app.state.store.create_run(request.task_id, request.idempotency_key, None)
    await executor.execute(run.run_id, request)
    done = await app.state.store.get_run(run.run_id)
    assert done.final_answer == 'draft initialized'
    selection = await executor.mcp_registry.tools_for_groups({'project_read'})
    tool = next(t for t in selection.tools if t.spec.name.endswith('__project_get'))
    result = await tool.handler(scope)
    assert result.ok
    assert json.loads(result.content)['status'] == 'draft'
    await executor.mcp_registry.aclose()
