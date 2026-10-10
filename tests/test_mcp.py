import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from contracts.models import CreateRunRequest, ToolCall
from harness.config import HarnessSettings
from harness.context import ContextBuilder
from harness.executor import RunExecutor
from harness.mcp_tools import (
    McpConfigurationError,
    McpServerConfig,
    McpToolRegistry,
    load_mcp_servers,
)
from harness.provider import ProviderDelta, ScriptedMockProvider
from harness.store import InMemorySessionStore
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy


class FakeMcpClient:
    def __init__(self, calls):
        self.calls = calls

    async def list_tools(self, *, cursor=None):
        self.calls.append(("list", cursor))
        return SimpleNamespace(
            tools=[SimpleNamespace(
                name="lookup",
                title="Lookup",
                description="Find a document.",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            )],
            next_cursor=None,
        )

    async def call_tool(self, name, arguments):
        self.calls.append(("call", name, arguments))
        return SimpleNamespace(
            content=[SimpleNamespace(text=f"found:{arguments['query']}")],
            structured_content=None,
            is_error=False,
        )


def fake_factory(calls):
    @asynccontextmanager
    async def factory(_server):
        calls.append(("open",))
        yield FakeMcpClient(calls)

    return factory


def write_config(path):
    path.write_text(
        """version: 1
servers:
  docs:
    transport: stdio
    command: python
    args: [server.py]
    cwd: ./tools
    env_from:
      DOCS_TOKEN: AGENT_X_DOCS_TOKEN
    tool_groups: [code_doc]
    allowed_tools: [lookup]
    risk: read_only
    parallel_safe: true
    idempotent: true
  remote:
    transport: streamable_http
    url: https://mcp.example.test/mcp
    headers_from_env:
      Authorization: AGENT_X_MCP_AUTH
    tool_groups: [knowledge]
    risk: read_only
""",
        encoding="utf-8",
    )


def test_mcp_config_supports_stdio_http_and_secret_references(tmp_path):
    config = tmp_path / "mcp_servers.yaml"
    write_config(config)
    servers = load_mcp_servers(config)
    assert [server.name for server in servers] == ["docs", "remote"]
    assert servers[0].cwd == (tmp_path / "tools").resolve()
    assert servers[0].env_from == {"DOCS_TOKEN": "AGENT_X_DOCS_TOKEN"}
    assert servers[1].headers_from_env == {"Authorization": "AGENT_X_MCP_AUTH"}


def test_mcp_config_rejects_inline_or_invalid_transport_shape(tmp_path):
    config = tmp_path / "mcp_servers.yaml"
    config.write_text(
        "version: 1\nservers:\n  bad:\n    transport: stdio\n    tool_groups: [docs]\n",
        encoding="utf-8",
    )
    with pytest.raises(McpConfigurationError, match="needs command"):
        load_mcp_servers(config)

    config.write_text(
        """version: 1
servers:
  bad:
    transport: stdio
    command: python
    tool_groups: [docs]
    risk: read_only
    env:
      TOKEN: do-not-store-secrets-here
""",
        encoding="utf-8",
    )
    with pytest.raises(McpConfigurationError, match="unknown or forbidden fields: env"):
        load_mcp_servers(config)


@pytest.mark.asyncio
async def test_registry_namespaces_filters_caches_and_calls_tools(tmp_path):
    config = tmp_path / "mcp_servers.yaml"
    write_config(config)
    calls = []
    registry = McpToolRegistry(
        load_mcp_servers(config), client_factory=fake_factory(calls), schema_cache_ttl_seconds=300
    )

    assert not (await registry.tools_for_groups({"other"})).tools
    first = await registry.tools_for_groups({"code_doc"})
    second = await registry.tools_for_groups({"code_doc"})
    assert not first.diagnostics
    assert [tool.spec.name for tool in first.tools] == ["mcp__docs__lookup"]
    assert first.tools[0].spec.source == "mcp:docs"
    assert len([call for call in calls if call[0] == "list"]) == 1
    assert [tool.spec.name for tool in second.tools] == ["mcp__docs__lookup"]

    result = await first.tools[0].handler({"query": "MCP"})
    assert result.ok and result.content == "found:MCP"
    assert ("call", "lookup", {"query": "MCP"}) in calls


@pytest.mark.asyncio
async def test_real_sdk_stdio_discovery_and_call():
    server_script = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    registry = McpToolRegistry((McpServerConfig(
        name="echo",
        transport="stdio",
        command=sys.executable,
        args=(str(server_script),),
        tool_groups=("test",),
        timeout_seconds=10,
    ),), discovery_timeout_seconds=10)

    selection = await registry.tools_for_groups({"test"})
    assert not selection.diagnostics
    assert [tool.spec.name for tool in selection.tools] == ["mcp__echo__echo"]
    result = await selection.tools[0].handler({"value": "hello"})
    assert result.ok and result.content == "mcp:hello"


@pytest.mark.asyncio
async def test_selected_skill_mounts_its_mcp_tool_for_the_run(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    skill = tmp_path / ".opencode" / "skills" / "code-doc"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: code-doc\ndescription: Search code documentation.\n"
        "tool_groups: [code_doc]\n---\n# Use the docs tool.\n",
        encoding="utf-8",
    )
    config = tmp_path / "mcp_servers.yaml"
    write_config(config)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("SKILL_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("SKILL_GLOBAL_CONFIG_ROOT", str(tmp_path / "global"))
    monkeypatch.setenv("MCP_CONFIG_PATH", str(config))
    settings = HarnessSettings.from_environment()
    calls = []
    registry = McpToolRegistry(
        load_mcp_servers(config), client_factory=fake_factory(calls), schema_cache_ttl_seconds=300
    )
    tool_call = ToolCall(name="mcp__docs__lookup", arguments={"query": "Agent-X"})
    provider = ScriptedMockProvider([
        [ProviderDelta(tool_calls=[tool_call], finish_reason="tool_calls")],
        [ProviderDelta(text="done", finish_reason="stop")],
    ])
    store = InMemorySessionStore()
    policy = ToolPolicy()
    executor = RunExecutor(
        store,
        provider,
        ContextBuilder(),
        ToolExecutor(ToolCatalog(), policy),
        policy,
        settings=settings,
        mcp_registry=registry,
    )
    request = CreateRunRequest(
        task_id="task_mcp",
        idempotency_key="mcp-one",
        input="search",
        selected_skills=["code-doc"],
    )
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    await executor.execute(run.run_id, request)

    assert (await store.get_run(run.run_id)).final_answer == "done"
    event_types = [event.type for event in await store.events_after(run.run_id)]
    assert "mcp.tools.mounted" in event_types
    assert ("call", "lookup", {"query": "Agent-X"}) in calls
