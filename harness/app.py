from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from contracts.models import CreateRunRequest, RunStatus, ToolResult, ToolSpec, new_id
from harness.config import HarnessSettings
from harness.context import ContextBuilder, ContextPolicy, ProviderSummaryGenerator
from harness.executor import RunExecutor
from harness.provider import ProviderAdapter, provider_from_settings
from harness.store import InMemorySessionStore
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy
from harness.skills import discover_skills


async def _echo_tool(arguments: dict) -> ToolResult:
    return ToolResult(ok=True, content=json.dumps(arguments, ensure_ascii=False))


def default_catalog(settings: HarnessSettings) -> ToolCatalog:
    catalog = ToolCatalog()
    catalog.register(ToolSpec(name="echo", description="Returns supplied JSON for testing.",
                              input_schema={"type": "object"}, parallel_safe=True, idempotent=True,
                              timeout_seconds=settings.tool_timeout_seconds,
                              max_result_bytes=settings.tool_max_result_bytes), _echo_tool)
    return catalog


def create_app(*, provider: ProviderAdapter | None = None, catalog: ToolCatalog | None = None,
               settings: HarnessSettings | None = None) -> FastAPI:
    app = FastAPI(title="TroubleShooter Harness", version="0.1.0")
    settings = settings or HarnessSettings.from_environment()
    store = InMemorySessionStore()
    policy = ToolPolicy(allow_writes=False)
    selected_provider = provider or provider_from_settings(settings)
    context_policy = ContextPolicy(
        summary_enabled=True,
        compaction_trigger_ratio=settings.context_compaction_trigger_ratio,
        compaction_target_ratio=settings.context_compaction_target_ratio,
        recent_blocks_to_keep=settings.context_recent_blocks_to_keep,
        tool_result_max_chars=settings.context_tool_result_max_chars,
        summary_max_input_tokens=settings.context_summary_max_input_tokens,
        summary_max_output_tokens=settings.context_summary_max_output_tokens,
        summary_timeout_seconds=settings.context_summary_timeout_seconds,
    )
    executor = RunExecutor(store, selected_provider,
                           ContextBuilder(policy=context_policy, summary_generator=ProviderSummaryGenerator(selected_provider)),
                           ToolExecutor(catalog or default_catalog(settings), policy, settings.tool_max_concurrency), policy,
                           log_root=settings.log_root, workspace_root=settings.workspace_root, settings=settings)
    app.state.settings = settings
    app.state.store = store
    app.state.executor = executor
    app.state.requests: dict[str, CreateRunRequest] = {}
    app.state.tasks: dict[str, asyncio.Task] = {}
    internal_token = os.getenv("HARNESS_INTERNAL_TOKEN")

    async def authenticate(x_internal_token: str | None = Header(default=None)) -> None:
        if internal_token and x_internal_token != internal_token:
            raise HTTPException(status_code=401, detail="invalid_internal_token")

    def schedule(run_id: str, request: CreateRunRequest) -> None:
        if run_id not in app.state.tasks or app.state.tasks[run_id].done():
            app.state.tasks[run_id] = asyncio.create_task(executor.execute(run_id, request))

    @app.post("/internal/runs", dependencies=[Depends(authenticate)], status_code=202)
    async def create_run(request: CreateRunRequest):
        changes = {}
        if "model" not in request.model_fields_set:
            changes["model"] = settings.model
        if "budget" not in request.model_fields_set:
            changes["budget"] = settings.default_budget()
        if changes:
            request = request.model_copy(update=changes)
        record, created = await store.create_run(request.task_id, request.idempotency_key, request.session_id, request.workspace_id)
        if created:
            app.state.requests[record.run_id] = request
            await store.append_event(record.run_id, "run.accepted", {})
            schedule(record.run_id, request)
        return record.view()

    @app.get("/internal/runs/{run_id}", dependencies=[Depends(authenticate)])
    async def read_run(run_id: str):
        try:
            return (await store.get_run(run_id)).view()
        except KeyError:
            raise HTTPException(404, "run_not_found")

    @app.get("/internal/runs/{run_id}/session", dependencies=[Depends(authenticate)])
    async def read_session(run_id: str):
        try:
            record = await store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, "run_not_found")
        return await store.session_entries(record.session_id)

    @app.get("/internal/skills", dependencies=[Depends(authenticate)])
    async def list_skills():
        snapshot = discover_skills(settings.skill_project_root, settings.skill_global_config_root,
            project_roots=(".opencode/skills", ".claude/skills", ".agents/skills"),
            global_roots=("opencode/skills", "../.claude/skills", "../.agents/skills"))
        if settings.skill_load_policy == "deny":
            return []
        return [summary.model_dump() for summary in snapshot.summaries()]

    @app.post("/internal/runs/{run_id}/cancel", dependencies=[Depends(authenticate)])
    async def cancel_run(run_id: str):
        try:
            record = await store.cancel(run_id)
        except KeyError:
            raise HTTPException(404, "run_not_found")
        task = app.state.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        return record.view()

    @app.post("/internal/runs/{run_id}/resume", dependencies=[Depends(authenticate)])
    async def resume_run(run_id: str):
        # This endpoint is an explicit user retry. It starts a fresh Session, rather than
        # replaying an uncertain tool call inside the old Session.
        try:
            record = await store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, "run_not_found")
        if record.status not in {RunStatus.interrupted, RunStatus.failed, RunStatus.cancelled}:
            raise HTTPException(409, "run_not_resumable")
        previous = app.state.requests.get(run_id)
        if not previous:
            raise HTTPException(409, "resume_request_not_available")
        request = previous.model_copy(update={"idempotency_key": new_id("resume"), "session_id": None})
        replacement, _ = await store.create_run(request.task_id, request.idempotency_key, request.session_id, request.workspace_id)
        app.state.requests[replacement.run_id] = request
        await store.append_event(replacement.run_id, "run.accepted", {"resumed_from_run_id": run_id})
        schedule(replacement.run_id, request)
        return replacement.view()

    async def event_stream(run_id: str, after_sequence: int) -> AsyncIterator[str]:
        cursor = after_sequence
        while True:
            events = await store.wait_for_events(run_id, cursor)
            for event in events:
                cursor = event.sequence
                yield f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
            record = await store.get_run(run_id)
            if record.status in {RunStatus.completed, RunStatus.failed, RunStatus.cancelled, RunStatus.interrupted}:
                return
            if not events:
                yield ": heartbeat\n\n"

    @app.get("/internal/runs/{run_id}/events", dependencies=[Depends(authenticate)])
    async def run_events(run_id: str, after_sequence: int = Query(default=0, ge=0)):
        try:
            await store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, "run_not_found")
        return StreamingResponse(event_stream(run_id, after_sequence), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
