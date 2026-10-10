import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from contracts.models import CreateRunRequest, ToolCall, ToolResult, ToolSpec, Usage
from harness.app import create_app
from harness.config import HarnessSettings
from harness.context import ContextBuilder
from harness.executor import RunExecutor
from harness.provider import ProviderDelta, ScriptedMockProvider
from harness.store import InMemorySessionStore
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy


async def build_executor(turns, handler=None):
    catalog = ToolCatalog()
    async def default_handler(args):
        return ToolResult(ok=True, content=f"result:{args['value']}")
    catalog.register(ToolSpec(name="lookup", description="test", input_schema={"type": "object"}, parallel_safe=True), handler or default_handler)
    store = InMemorySessionStore()
    policy = ToolPolicy()
    return store, RunExecutor(store, ScriptedMockProvider(turns), ContextBuilder(), ToolExecutor(catalog, policy), policy)


@pytest.mark.asyncio
async def test_multi_turn_tool_run_preserves_order_and_events():
    call = ToolCall(name="lookup", arguments={"value": "a"})
    store, executor = await build_executor([
        [ProviderDelta(text="checking "), ProviderDelta(tool_calls=[call], finish_reason="tool_calls")],
        [ProviderDelta(text="done", usage=Usage(prompt_tokens_actual=11, completion_tokens_actual=2, finish_reason="stop"))],
    ])
    request = CreateRunRequest(task_id="task_1", idempotency_key="one", input="find it")
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    await executor.execute(run.run_id, request)
    saved = await store.get_run(run.run_id)
    assert saved.status.value == "completed"
    assert saved.final_answer == "done"
    types = [event.type for event in await store.events_after(run.run_id)]
    assert types.index("tool.started") < types.index("tool.completed") < types.index("run.completed")
    entries = await store.session_entries(run.session_id)
    assert [entry.role for entry in entries] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_cancelled_run_has_terminal_event():
    async def slow_tool(args):
        await asyncio.sleep(5)
        return ToolResult(ok=True, content="never")
    call = ToolCall(name="lookup", arguments={"value": "a"})
    store, executor = await build_executor([[ProviderDelta(tool_calls=[call])]], slow_tool)
    request = CreateRunRequest(task_id="task_2", idempotency_key="two", input="cancel")
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    task = asyncio.create_task(executor.execute(run.run_id, request))
    await asyncio.sleep(.02)
    await store.cancel(run.run_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.get_run(run.run_id)).status.value == "cancelled"
    assert (await store.events_after(run.run_id))[-1].type == "run.cancelled"


@pytest.mark.asyncio
async def test_context_budget_fails_diagnostically():
    store, executor = await build_executor([[ProviderDelta(text="unused")]])
    request = CreateRunRequest(task_id="task_3", idempotency_key="three", input="x" * 10000,
                               budget={"context_window": 1024, "output_reserve": 512})
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    await executor.execute(run.run_id, request)
    assert (await store.get_run(run.run_id)).stop_reason == "context_budget_exceeded"


@pytest.mark.asyncio
async def test_runtime_workspace_and_logs_are_isolated_by_session(tmp_path):
    catalog = ToolCatalog()
    store = InMemorySessionStore()
    policy = ToolPolicy()
    executor = RunExecutor(
        store, ScriptedMockProvider([[ProviderDelta(text="done")]]), ContextBuilder(),
        ToolExecutor(catalog, policy), policy,
        log_root=tmp_path / "logs", workspace_root=tmp_path / "workspaces",
    )
    request = CreateRunRequest(task_id="task_logs", idempotency_key="logs", input="do not log this prompt")
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    await executor.execute(run.run_id, request)
    assert (tmp_path / "workspaces" / run.session_id).is_dir()
    log = (tmp_path / "logs" / run.session_id / f"harness_{run.run_id}.jsonl").read_text()
    assert "run.completed" in log
    assert "do not log this prompt" not in log


def test_harness_app_with_runtime_settings_completes_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("SKILL_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("SKILL_GLOBAL_CONFIG_ROOT", str(tmp_path / "config"))
    app = create_app(settings=HarnessSettings.from_environment())

    with TestClient(app) as client:
        created = client.post("/internal/runs", json={
            "task_id": "task_app", "idempotency_key": "app-run", "input": "hello",
        })
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/internal/runs/{run_id}").json()
            if run["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.01)

    assert run["status"] == "completed"
    assert run["final_answer"] == "Mock provider received: hello"
