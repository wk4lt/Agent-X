import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from contracts.models import CreateRunRequest, RunStatus, ToolCall, ToolResult, ToolSpec, Usage
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
    progress = [event.payload for event in await store.events_after(run.run_id) if event.type == "run.progress"]
    assert any(item["round"] == 1 and item["phase"] == "context" for item in progress)
    assert any(item["round"] == 1 and item["phase"] == "tools" for item in progress)
    assert any(item["round"] == 2 and item["phase"] == "answer" for item in progress)
    entries = await store.session_entries(run.session_id)
    assert [entry.role for entry in entries] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_answer_duration_includes_queue_and_tools_and_is_frozen():
    async def tool(args):
        await asyncio.sleep(.02)
        return ToolResult(ok=True, content="found")

    store, executor = await build_executor([
        [ProviderDelta(tool_calls=[ToolCall(name="lookup", arguments={})], usage=Usage(elapsed_ms=1))],
        [ProviderDelta(text="done", usage=Usage(elapsed_ms=1))],
    ], tool)
    request = CreateRunRequest(task_id="duration", idempotency_key="duration", input="find it")
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    # Simulate time spent accepted, before executor acquires the session lock.
    run.created_monotonic -= 1
    await executor.execute(run.run_id, request)
    elapsed = run.view().elapsed_ms
    assert elapsed is not None and elapsed >= 1020
    events = await store.events_after(run.run_id)
    assert events[-1].type == "run.completed"
    assert events[-1].payload["elapsed_ms"] == elapsed
    assert sum(usage.elapsed_ms for usage in run.usages) == 2
    await store.update_run(run.run_id, status=RunStatus.completed)
    assert run.view().elapsed_ms == elapsed


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
    entries = await store.session_entries(run.session_id)
    assert [entry.role for entry in entries] == ["user", "assistant", "tool"]
    assert json.loads(entries[-1].content)["error_code"] == "tool_interrupted"


@pytest.mark.asyncio
async def test_runs_sharing_a_session_are_serialized_and_see_completed_history():
    class SerialProbeProvider:
        def __init__(self):
            self.requests = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.active = 0
            self.max_active = 0

        async def stream(self, request):
            index = len(self.requests)
            self.requests.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if index == 0:
                    self.started.set()
                    await self.release.wait()
                yield ProviderDelta(text=f"answer-{index + 1}")
            finally:
                self.active -= 1

    provider = SerialProbeProvider()
    store = InMemorySessionStore()
    policy = ToolPolicy()
    executor = RunExecutor(
        store, provider, ContextBuilder(), ToolExecutor(ToolCatalog(), policy), policy,
    )
    first_request = CreateRunRequest(task_id="first", idempotency_key="first", input="question one")
    first, _ = await store.create_run("first", "first", None)
    second_request = CreateRunRequest(
        task_id="second", idempotency_key="second", input="question two",
        session_id=first.session_id,
    )
    second, _ = await store.create_run("second", "second", first.session_id)

    first_task = asyncio.create_task(executor.execute(first.run_id, first_request))
    await provider.started.wait()
    second_task = asyncio.create_task(executor.execute(second.run_id, second_request))
    await asyncio.sleep(.02)
    assert len(provider.requests) == 1
    provider.release.set()
    await asyncio.gather(first_task, second_task)

    assert provider.max_active == 1
    second_contents = [message.get("content") for message in provider.requests[1].messages]
    assert second_contents[-3:] == ["question one", "answer-1", "question two"]


@pytest.mark.asyncio
async def test_context_budget_fails_diagnostically():
    store, executor = await build_executor([[ProviderDelta(text="unused")]])
    request = CreateRunRequest(task_id="task_3", idempotency_key="three", input="x" * 10000,
                               budget={"context_window": 1024, "output_reserve": 512})
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    await executor.execute(run.run_id, request)
    assert (await store.get_run(run.run_id)).stop_reason == "context_budget_exceeded"


@pytest.mark.asyncio
async def test_unexpected_provider_error_has_diagnostic_event_and_traceback(tmp_path):
    class FailingProvider:
        async def stream(self, request):
            raise RuntimeError("connection reset while streaming")
            yield  # pragma: no cover - makes this an async generator

    store = InMemorySessionStore()
    policy = ToolPolicy()
    executor = RunExecutor(store, FailingProvider(), ContextBuilder(), ToolExecutor(ToolCatalog(), policy), policy,
                           log_root=tmp_path / "logs")
    request = CreateRunRequest(task_id="task_failure", idempotency_key="failure", input="diagnose")
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)

    await executor.execute(run.run_id, request)

    saved = await store.get_run(run.run_id)
    assert saved.status.value == "failed"
    assert saved.stop_reason == "provider_error"
    assert saved.failure_stage == "provider_stream"
    assert saved.failure_code == "RuntimeError"
    assert saved.failure_message == "connection reset while streaming"
    failed = [event for event in await store.events_after(run.run_id) if event.type == "run.failed"][-1]
    assert failed.payload["error_code"] == "RuntimeError"
    assert "traceback" not in failed.payload
    log = (tmp_path / "logs" / run.session_id / f"harness_{run.run_id}.jsonl").read_text()
    assert '"event":"run.failed"' in log
    assert "RuntimeError: connection reset while streaming" in log


@pytest.mark.asyncio
async def test_runtime_workspace_and_logs_are_isolated_by_session(tmp_path):
    catalog = ToolCatalog()
    store = InMemorySessionStore()
    policy = ToolPolicy()
    provider = ScriptedMockProvider([[ProviderDelta(text="done")]])
    executor = RunExecutor(
        store, provider, ContextBuilder(),
        ToolExecutor(catalog, policy), policy,
        log_root=tmp_path / "logs", workspace_root=tmp_path / "workspaces",
    )
    request = CreateRunRequest(task_id="task_logs", idempotency_key="logs", input="do not log this prompt",
                               workspace_id="conversation_1")
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None, request.workspace_id)
    await executor.execute(run.run_id, request)
    workspace = (tmp_path / "workspaces" / "conversation_1").resolve()
    assert workspace.is_dir()
    system_messages = [item["content"] for item in provider.requests[0].messages if item["role"] == "system"]
    assert any(str(workspace) in item and "AGENT_X_WORKSPACE" in item for item in system_messages)
    log = (tmp_path / "logs" / run.session_id / f"harness_{run.run_id}.jsonl").read_text()
    assert "run.completed" in log
    assert "do not log this prompt" not in log


def test_harness_app_with_runtime_settings_completes_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("SKILL_PROJECT_ROOT", str(tmp_path))
    skill = tmp_path / "skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Demo software tasks.\n---\n# Instructions\n")
    app = create_app(settings=HarnessSettings.from_environment())

    with TestClient(app) as client:
        created = client.post("/internal/runs", json={
            "task_id": "task_app", "idempotency_key": "app-run", "input": "hello", "selected_skills": ["demo"],
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
