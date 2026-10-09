import asyncio

import pytest

from contracts.models import DerivedSummary, RunBudget, SessionEntry, ToolCall, ToolResult, ToolSpec, Usage
from harness.context import ContextBudgetExceeded, ContextBuilder, ContextPolicy, SummaryGenerator
from harness.executor import RunExecutor
from harness.provider import ProviderDelta, ScriptedMockProvider
from harness.store import InMemorySessionStore
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy


def budget(window: int = 8_000) -> RunBudget:
    return RunBudget(context_window=window, output_reserve=100, safety_margin=20)


def active_tools() -> list:
    async def lookup(arguments):
        return ToolResult(ok=True, content="ok")
    catalog = ToolCatalog()
    catalog.register(ToolSpec(name="lookup", description="Lookup by a very descriptive query schema.", input_schema={
        "type": "object", "properties": {"query": {"type": "string", "description": "long query"}}, "required": ["query"],
    }), lookup)
    return ToolPolicy().filter(catalog)


class StubSummary:
    def __init__(self, *, fail: bool = False):
        self.fail, self.calls = fail, []

    async def summarize(self, *, entries, model, target_tokens, max_input_tokens, max_output_tokens, timeout_seconds):
        self.calls.append([entry.id for entry in entries])
        if self.fail:
            raise RuntimeError("summary unavailable")
        return DerivedSummary(content="Old facts: decision A; open issue B.", source_entry_ids=[entry.id for entry in entries],
                              model=model, template="test", usage=Usage(prompt_tokens_actual=22, completion_tokens_actual=9))


@pytest.mark.asyncio
async def test_context_keeps_tool_call_result_pair_and_counts_structured_fields():
    call = ToolCall(name="lookup", arguments={"query": "x" * 600})
    entries = [SessionEntry(role="user", content="find a fact"), SessionEntry(role="assistant", content="", tool_calls=[call]),
               SessionEntry(role="tool", content='{"ok":true,"content":"fact"}', tool_call_id=call.id, tool_name="lookup"),
               SessionEntry(role="user", content="use the fact")]
    builder = ContextBuilder(policy=ContextPolicy(summary_enabled=False, recent_blocks_to_keep=2))
    request, report = await builder.build(task_id="task", run_id="run", turn_id="turn", model="mock", entries=entries, tools=active_tools(), budget=budget())
    messages = request.messages[2:]
    pair_start = next(index for index, message in enumerate(messages) if message.get("tool_calls"))
    assert messages[pair_start + 1]["role"] == "tool"
    assert messages[pair_start + 1]["tool_call_id"] == call.id
    assert report.tool_schema_tokens_est > 0
    assert report.history_tokens_est > 100  # includes serialized tool-call arguments, not only message text.
    assert report.user_input_tokens_est > 0
    assert report.input_tokens_est == report.system_tokens_est + report.workflow_skill_tokens_est + report.project_context_tokens_est + report.history_tokens_est + report.tool_schema_tokens_est + report.evidence_tokens_est + report.tool_result_tokens_est + report.user_input_tokens_est


@pytest.mark.asyncio
async def test_budget_trims_large_tool_result_without_mutating_session_log():
    call = ToolCall(name="lookup", arguments={"query": "q"})
    original = "tool-output-" * 600
    entries = [SessionEntry(role="user", content="old"), SessionEntry(role="assistant", content="", tool_calls=[call]),
               SessionEntry(role="tool", content=original, tool_call_id=call.id, tool_name="lookup"),
               SessionEntry(role="user", content="latest request")]
    builder = ContextBuilder(policy=ContextPolicy(summary_enabled=False, recent_blocks_to_keep=1, tool_result_max_chars=120))
    request, report = await builder.build(task_id="task", run_id="run", turn_id="turn", model="mock", entries=entries, tools=[], budget=budget(1_200))
    tool_message = next(message for message in request.messages if message["role"] == "tool")
    assert "truncated" in tool_message["content"]
    assert entries[2].content == original
    assert report.tool_result_reductions[0].entry_id == entries[2].id
    assert report.input_tokens_after_est < report.input_tokens_before_est


@pytest.mark.asyncio
async def test_untrimmable_context_raises_diagnostic_budget_error():
    builder = ContextBuilder(system_rules="mandatory-constraint " * 1_000,
                             policy=ContextPolicy(summary_enabled=False, recent_blocks_to_keep=1))
    with pytest.raises(ContextBudgetExceeded) as raised:
        await builder.build(task_id="task", run_id="run", turn_id="turn", model="mock",
                            entries=[SessionEntry(role="user", content="latest")], tools=[], budget=budget(1_200))
    assert raised.value.available_input_tokens == 1_080
    assert raised.value.estimated_input_tokens > raised.value.available_input_tokens
    assert raised.value.required_entry_ids


@pytest.mark.asyncio
async def test_compaction_summarizes_only_old_completed_plain_turns_and_is_traceable():
    call = ToolCall(name="lookup", arguments={"query": "preserve this pairing"})
    entries = [SessionEntry(role="user", content="old request " * 120), SessionEntry(role="assistant", content="old decision " * 120),
               SessionEntry(role="assistant", content="", tool_calls=[call]),
               SessionEntry(role="tool", content="tool evidence", tool_call_id=call.id, tool_name="lookup"),
               SessionEntry(role="user", content="latest constraint")]
    original = [entry.model_dump() for entry in entries]
    generated, events = [], []
    summary = StubSummary()
    builder = ContextBuilder(policy=ContextPolicy(compaction_trigger_ratio=.05, compaction_target_ratio=.02, recent_blocks_to_keep=2), summary_generator=summary)

    async def persist(item): generated.append(item)
    async def event(name, payload): events.append((name, payload))
    request, report = await builder.build(task_id="task", run_id="run", turn_id="turn", model="mock", entries=entries, tools=[], budget=budget(), persist_summary=persist, compaction_events=event)
    assert summary.calls == [[entries[0].id, entries[1].id]]
    assert generated[0].source_entry_ids == [entries[0].id, entries[1].id]
    assert [entry.model_dump() for entry in entries] == original
    assert entries[0].content not in " ".join(str(message) for message in request.messages)
    assert any("Prior-session derived summary" in message["content"] for message in request.messages if message["role"] == "system")
    tool_index = next(index for index, message in enumerate(request.messages) if message.get("tool_calls"))
    assert request.messages[tool_index + 1]["tool_call_id"] == call.id
    assert report.summary_source_entry_ids == [entries[0].id, entries[1].id]
    assert report.summary_usage_actual and report.summary_usage_actual.completion_tokens_actual == 9
    assert [name for name, _ in events] == ["context.compaction.started", "context.compaction.completed"]
    second_request, second_report = await builder.build(task_id="task", run_id="run", turn_id="turn-2", model="mock", entries=entries, tools=[], budget=budget(), summaries=generated)
    assert len(summary.calls) == 1
    assert entries[0].content not in " ".join(str(message) for message in second_request.messages)
    assert second_report.summary_refs[0].source_entry_ids == [entries[0].id, entries[1].id]


@pytest.mark.asyncio
async def test_summary_failure_falls_back_to_tool_trimming_and_records_event():
    call = ToolCall(name="lookup", arguments={"query": "q"})
    entries = [SessionEntry(role="user", content="old discussion"), SessionEntry(role="assistant", content="old response"),
               SessionEntry(role="assistant", content="", tool_calls=[call]), SessionEntry(role="tool", content="X" * 5_000, tool_call_id=call.id, tool_name="lookup"),
               SessionEntry(role="user", content="latest")]
    events = []
    builder = ContextBuilder(policy=ContextPolicy(compaction_trigger_ratio=.05, compaction_target_ratio=.02, recent_blocks_to_keep=1, tool_result_max_chars=100), summary_generator=StubSummary(fail=True))
    async def event(name, payload): events.append(name)
    request, report = await builder.build(task_id="task", run_id="run", turn_id="turn", model="mock", entries=entries, tools=[], budget=budget(1_200), compaction_events=event)
    assert "context.compaction.failed" in events
    assert report.summary_trigger_reason == "summary_failed_fallback"
    assert report.tool_result_reductions
    assert any(message["role"] == "tool" and "truncated" in message["content"] for message in request.messages)


@pytest.mark.asyncio
async def test_executor_emits_safe_estimated_and_actual_usage_events():
    store, policy = InMemorySessionStore(), ToolPolicy()
    provider = ScriptedMockProvider([[ProviderDelta(text="done", usage=Usage(prompt_tokens_actual=23, completion_tokens_actual=4, finish_reason="stop"))]])
    executor = RunExecutor(store, provider, ContextBuilder(policy=ContextPolicy(summary_enabled=False)), ToolExecutor(ToolCatalog(), policy), policy)
    from contracts.models import CreateRunRequest
    request = CreateRunRequest(task_id="task", idempotency_key="status", input="hello", budget=budget())
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    await executor.execute(run.run_id, request)
    events = await store.events_after(run.run_id)
    context = next(event.payload for event in events if event.type == "context.built")
    usage = next(event.payload for event in events if event.type == "usage.updated")
    assert context["status"] == "ready" and "input_tokens_after_est" in context
    assert "hello" not in str(context)
    assert usage["kind"] == "run" and usage["prompt_tokens_actual"] == 23


@pytest.mark.asyncio
async def test_executor_persists_summary_and_emits_compaction_and_summary_usage_events():
    store, policy = InMemorySessionStore(), ToolPolicy()
    provider = ScriptedMockProvider([[ProviderDelta(text="done", usage=Usage(prompt_tokens_actual=31, completion_tokens_actual=5, finish_reason="stop"))]])
    summary = StubSummary()
    builder = ContextBuilder(policy=ContextPolicy(compaction_trigger_ratio=.05, compaction_target_ratio=.02, recent_blocks_to_keep=1), summary_generator=summary)
    executor = RunExecutor(store, provider, builder, ToolExecutor(ToolCatalog(), policy), policy)
    from contracts.models import CreateRunRequest
    request = CreateRunRequest(task_id="task", idempotency_key="summary-events", input="latest constraint", budget=budget())
    run, _ = await store.create_run(request.task_id, request.idempotency_key, None)
    await store.append_entry(run.session_id, SessionEntry(role="user", content="old request " * 120))
    await store.append_entry(run.session_id, SessionEntry(role="assistant", content="old answer " * 120))
    await executor.execute(run.run_id, request)
    events = await store.events_after(run.run_id)
    names = [event.type for event in events]
    assert "context.compaction.started" in names and "context.compaction.completed" in names
    assert any(event.type == "usage.updated" and event.payload["kind"] == "summary" for event in events)
    assert len(await store.session_summaries(run.session_id)) == 1
