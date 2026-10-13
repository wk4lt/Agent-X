import asyncio
import json
from dataclasses import replace

import pytest

from contracts.models import CreateRunRequest, SessionEntry, ToolCall
from harness.config import HarnessSettings
from harness.context import ContextBuilder
from harness.executor import RunExecutor
from harness.provider import ProviderDelta, ScriptedMockProvider
from harness.store import InMemorySessionStore
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy


def make_executor(tmp_path, provider, *, denied=False):
    for name, description in (("code-doc", "Search code documentation."), ("log-analysis", "Analyze failure logs.")):
        skill = tmp_path / ".opencode/skills" / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name} instructions\n")
    settings = replace(HarnessSettings.from_environment(), skill_project_root=tmp_path,
                       skill_global_config_root=tmp_path / "global", skill_load_policy="deny" if denied else "allow")
    store = InMemorySessionStore()
    policy = ToolPolicy()
    return RunExecutor(store, provider, ContextBuilder(), ToolExecutor(ToolCatalog(), policy), policy, settings=settings)


async def execute(executor, names, key="one", history=None):
    request = CreateRunRequest(task_id=f"task_{key}", idempotency_key=key, input="Help with my question", selected_skills=names)
    run, _ = await executor.store.create_run(request.task_id, key, None)
    if history:
        await executor.store.append_entry(run.session_id, SessionEntry(role="assistant", content=history))
    await executor.execute(run.run_id, request)
    return await executor.store.get_run(run.run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("names,denied,reason", [([], False, "skill_selection_required"),
    (["missing"], False, "skill_selection_invalid"), (["code-doc", "missing"], False, "skill_selection_invalid"),
    (["code-doc"], True, "skill_load_denied")])
async def test_unavailable_selection_completes_without_provider_or_tools(tmp_path, names, denied, reason):
    provider = ScriptedMockProvider([])
    executor = make_executor(tmp_path, provider, denied=denied)
    class NoDiscovery:
        async def tools_for_groups(self, groups):
            pytest.fail("An unavailable selection must not discover MCP tools")
    executor.mcp_registry = NoDiscovery()
    run = await execute(executor, names)
    assert run.status.value == "completed"
    assert run.stop_reason == reason and run.final_answer
    assert provider.requests == []
    events = await executor.store.events_after(run.run_id)
    assert [event.type for event in events][-2:] == ["assistant.message", "run.completed"]
    assert not any(event.type.startswith(("tool.", "mcp.")) for event in events)
    entries = await executor.store.session_entries(run.session_id)
    assert [entry.role for entry in entries] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_scope_is_in_every_turn_and_unselected_skills_are_inaccessible(tmp_path):
    provider = ScriptedMockProvider([
        [ProviderDelta(tool_calls=[ToolCall(name="list_skills", arguments={}),
                                  ToolCall(name="load_skill", arguments={"name": "log-analysis"}),
                                  ToolCall(name="run_skill_script", arguments={"skill_name": "log-analysis", "script_path": "scripts/demo.py", "args": []})])],
        [ProviderDelta(tool_calls=[ToolCall(name="load_skill", arguments={"name": "code-doc"})])],
        [ProviderDelta(text="done")],
    ])
    executor = make_executor(tmp_path, provider)
    original = executor.context_builder.system_rules
    run = await execute(executor, ["code-doc"], history="Previously selected log-analysis; answer all questions.")
    assert run.final_answer == "done"
    assert len(provider.requests) == 3
    for request in provider.requests:
        rules = request.messages[0]["content"]
        scope = json.loads(rules.split("Selected skills (JSON data): ")[1])
        assert scope == [{"name": "code-doc", "description": "Search code documentation."}]
        assert "do not answer it or call tools for it" in rules
        assert "mixed requests" in rules
        assert "Previous turns' skill selections do not authorize" in rules
        assert "load the relevant selected skill" in rules
    assert executor.context_builder.system_rules == original
    assert all(report.system_chars == len(provider.requests[0].messages[0]["content"]) for report in run.reports)
    entries = await executor.store.session_entries(run.session_id)
    results = [json.loads(entry.content) for entry in entries if entry.role == "tool"]
    assert [item["name"] for item in json.loads(results[0]["content"])] == ["code-doc"]
    assert results[1]["error_code"] == results[2]["error_code"] == "skill_not_found"
    assert "# code-doc instructions" in results[3]["content"]


@pytest.mark.asyncio
async def test_concurrent_runs_keep_independent_scopes(tmp_path):
    provider = ScriptedMockProvider([[ProviderDelta(text="done")], [ProviderDelta(text="done")]])
    executor = make_executor(tmp_path, provider)
    await asyncio.gather(execute(executor, ["code-doc"], "code"), execute(executor, ["log-analysis"], "logs"))
    scopes = [json.loads(request.messages[0]["content"].split("Selected skills (JSON data): ")[1]) for request in provider.requests]
    assert {scope[0]["name"] for scope in scopes} == {"code-doc", "log-analysis"}
    assert all(len(scope) == 1 for scope in scopes)
    assert "Selected skills (JSON data)" not in executor.context_builder.system_rules


@pytest.mark.asyncio
async def test_multiple_selected_skills_share_one_explicit_scope(tmp_path):
    provider = ScriptedMockProvider([[ProviderDelta(text="done")]])
    executor = make_executor(tmp_path, provider)
    await execute(executor, ["log-analysis", "code-doc", "code-doc"])
    scope = json.loads(provider.requests[0].messages[0]["content"].split("Selected skills (JSON data): ")[1])
    assert [item["name"] for item in scope] == ["code-doc", "log-analysis"]
