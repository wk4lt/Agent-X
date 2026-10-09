from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from contracts.models import (
    CreateRunRequest, RunStatus, SessionEntry, ToolCall, ToolResult, Usage, new_id,
)
from contracts.session_storage import append_session_log, session_directory
from harness.context import ContextBudgetExceeded, ContextBuilder
from harness.provider import ProviderAdapter
from harness.skills import SkillScriptRunner, discover_skills
from harness.store import InMemorySessionStore
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy


@dataclass
class RunExecutor:
    store: InMemorySessionStore
    provider: ProviderAdapter
    context_builder: ContextBuilder
    tool_executor: ToolExecutor
    policy: ToolPolicy
    log_root: Path | None = None
    workspace_root: Path | None = None
    settings: object | None = None

    async def execute(self, run_id: str, request: CreateRunRequest) -> None:
        run = await self.store.get_run(run_id)
        started = time.monotonic()
        self._ensure_workspace(run)
        await self.store.update_run(run_id, status=RunStatus.running)
        self._log(run, "run.started", task_id=run.task_id, run_id=run_id, model=request.model)
        await self.emit(run_id, "run.started", {})
        await self.store.append_entry(run.session_id, SessionEntry(role="user", content=request.input))
        settings = getattr(self, "settings", None)
        run_tools = self.tool_executor
        if settings is not None:
            snapshot = discover_skills(settings.skill_project_root, settings.skill_global_config_root,
                project_roots=(".opencode/skills", ".claude/skills", ".agents/skills"),
                global_roots=("opencode/skills", "../.claude/skills", "../.agents/skills"))
            if request.selected_skills:
                snapshot.records = {name: record for name, record in snapshot.records.items() if name in set(request.selected_skills)}
            for summary in snapshot.summaries():
                await self.emit(run_id, "skill.discovered", {"name": summary.name, "source": summary.source,
                    "trust_level": summary.trust_level, "content_hash": snapshot.records[summary.name].content_hash})
            for diagnostic in snapshot.diagnostics:
                await self.emit(run_id, "skill.invalid", {"error_code": diagnostic.error_code, "path": diagnostic.path})
            catalog = ToolCatalog()
            for registered in self.tool_executor.catalog.active(allow_writes=True):
                catalog.register(registered.spec, registered.handler)
            loaded_hashes: set[str] = set()
            runner = SkillScriptRunner(workspace_root=settings.skill_project_root, python=os.getenv("PYTHON_EXECUTABLE", sys.executable),
                policy=settings.skill_script_policy, allowed_extensions=settings.skill_allowed_extensions,
                default_timeout=settings.skill_default_timeout_seconds, max_timeout=settings.skill_max_timeout_seconds,
                max_output_bytes=settings.skill_max_output_bytes, max_concurrent=settings.skill_max_concurrent_scripts_per_run)

            async def list_skills(_arguments: dict) -> ToolResult:
                visible = [] if settings.skill_load_policy == "deny" else [item.model_dump() for item in snapshot.summaries()]
                return ToolResult(ok=True, content=json.dumps(visible, ensure_ascii=False))

            async def load_skill(arguments: dict) -> ToolResult:
                name = arguments.get("name")
                if not isinstance(name, str):
                    return ToolResult(ok=False, content="name is required.", error_code="invalid_arguments")
                if settings.skill_load_policy == "deny":
                    await self.emit(run_id, "skill.load_denied", {"name": name, "error_code": "skill_load_denied"})
                    return ToolResult(ok=False, content="Skill loading is denied by policy.", error_code="skill_load_denied")
                try:
                    content = snapshot.load(name)
                except KeyError:
                    await self.emit(run_id, "skill.load_denied", {"name": name, "error_code": "skill_not_found"})
                    return ToolResult(ok=False, content="Skill was not found.", error_code="skill_not_found")
                if content.content_hash in loaded_hashes:
                    return ToolResult(ok=True, content="Skill is already loaded for this Run.")
                loaded_hashes.add(content.content_hash)
                await self.emit(run_id, "skill.loaded", {"name": name, "source": snapshot.records[name].summary.source,
                    "content_hash": content.content_hash})
                return ToolResult(ok=True, content=content.content)

            async def run_skill_script(arguments: dict) -> ToolResult:
                name, script = arguments.get("skill_name"), arguments.get("script_path")
                if not isinstance(name, str) or not isinstance(script, str):
                    return ToolResult(ok=False, content="skill_name and script_path are required.", error_code="invalid_arguments")
                record = snapshot.records.get(name)
                if not record:
                    return ToolResult(ok=False, content="Skill was not found.", error_code="skill_not_found")
                if settings.skill_script_policy == "ask" and record.summary.trust_level != "trusted":
                    await self.emit(run_id, "skill.script.approval_required", {"skill": name, "script": script})
                await self.emit(run_id, "skill.script.started", {"skill": name, "script": script,
                    "script_hash": _safe_script_hash(record.root, script), "args_count": len(arguments.get("args", []))})
                result = await runner.run(record, script, arguments.get("args", []), arguments.get("timeout_seconds"))
                await self.emit(run_id, "skill.script.completed" if result.ok else "skill.script.failed", {"skill": name,
                    "script": script, "error_code": result.error_code, "output_bytes": len(result.content.encode()), "truncated": result.truncated})
                return result

            catalog.register(ToolSpec(name="list_skills", description="List available OpenCode-compatible Skills by name and description.",
                input_schema={"type": "object", "properties": {}}, parallel_safe=True), list_skills)
            catalog.register(ToolSpec(name="load_skill", description="Load an available Skill by exact name. Loading does not execute scripts.",
                input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}), load_skill)
            catalog.register(ToolSpec(name="run_skill_script", description="Execute an allowed script inside a loaded/discovered Skill using argv arguments.",
                input_schema={"type": "object", "properties": {"skill_name": {"type": "string"}, "script_path": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}, "timeout_seconds": {"type": "number"}}, "required": ["skill_name", "script_path", "args"]}), run_skill_script)
            run_tools = ToolExecutor(catalog, self.policy, settings.tool_max_concurrency)
        tool_calls_used = 0
        try:
            for turn_number in range(1, request.budget.max_turns + 1):
                if run.cancelled:
                    await self._cancel(run_id)
                    return
                if time.monotonic() - started > request.budget.wall_time_seconds:
                    await self._fail(run_id, "budget_exceeded")
                    return
                turn_id = new_id("turn")
                active = self.policy.filter(run_tools.catalog)
                try:
                    async def persist_summary(summary):
                        await self.store.append_summary(run.session_id, summary)
                        if summary.usage:
                            run.summary_usages.append(summary.usage)
                            await self.emit(run_id, "usage.updated", {"kind": "summary", **summary.usage.model_dump(exclude_none=True)})

                    async def compaction_event(event_type: str, payload: dict) -> None:
                        await self.emit(run_id, event_type, payload)

                    provider_request, report = await self.context_builder.build(
                        task_id=run.task_id, run_id=run_id, turn_id=turn_id, model=request.model,
                        entries=await self.store.session_entries(run.session_id), tools=active, budget=request.budget,
                        summaries=await self.store.session_summaries(run.session_id),
                        persist_summary=persist_summary, compaction_events=compaction_event,
                    )
                except ContextBudgetExceeded as exc:
                    await self.emit(run_id, "context.built", {"turn_id": turn_id, "status": "failed", "error_code": "context_budget_exceeded", "available_input_tokens": exc.available_input_tokens, "estimated_input_tokens": exc.estimated_input_tokens, "required_entry_ids": exc.required_entry_ids})
                    await self._fail(run_id, "context_budget_exceeded")
                    return
                run.reports.append(report)
                self._log(run, "context.built", turn_id=turn_id, input_tokens_est=report.input_tokens_est,
                          active_tool_count=len(report.active_tool_names), omitted_entry_count=sum(len(ids) for ids in report.omitted_entry_ids_by_reason.values()))
                await self.emit(run_id, "context.built", {"turn_id": turn_id, "status": "ready", "input_tokens_before_est": report.input_tokens_before_est, "input_tokens_after_est": report.input_tokens_after_est, "available_input_tokens": report.available_input_tokens, "output_reserve": report.output_reserve, "context_window": report.context_window, "active_tool_names": report.active_tool_names, "omitted_entry_count": sum(len(ids) for ids in report.omitted_entry_ids_by_reason.values()), "tool_result_reduction_count": len(report.tool_result_reductions), "summary_refs": [item.summary_id for item in report.summary_refs]})
                text, calls, usage = await self._collect_response(run_id, provider_request)
                run.usages.append(usage)
                self._log(run, "llm.completed", turn_id=turn_id, ttft_ms=usage.ttft_ms,
                          elapsed_ms=usage.elapsed_ms, finish_reason=usage.finish_reason,
                          tool_call_count=len(calls))
                await self.emit(run_id, "usage.updated", {"kind": "run", **usage.model_dump(exclude_none=True)})
                if calls:
                    tool_calls_used += len(calls)
                    if tool_calls_used > request.budget.max_tool_calls:
                        await self._fail(run_id, "budget_exceeded")
                        return
                    await self.store.append_entry(run.session_id, SessionEntry(role="assistant", content=text, tool_calls=calls))
                    results = await run_tools.execute_batch(calls, lambda typ, payload: self.emit(run_id, typ, payload))
                    for call, result in zip(calls, results, strict=True):
                        await self.store.append_entry(run.session_id, SessionEntry(
                            role="tool", content=result.model_dump_json(), tool_call_id=call.id, tool_name=call.name))
                    continue
                await self.store.append_entry(run.session_id, SessionEntry(role="assistant", content=text))
                await self.store.update_run(run_id, status=RunStatus.completed, stop_reason="final_answer", final_answer=text)
                self._log(run, "run.completed", run_id=run_id, stop_reason="final_answer")
                await self.emit(run_id, "assistant.message", {"content": text})
                await self.emit(run_id, "run.completed", {"stop_reason": "final_answer"})
                return
            await self._fail(run_id, "max_turns")
        except asyncio.CancelledError:
            await self._cancel(run_id)
            raise
        except Exception:
            await self._fail(run_id, "provider_error")

    async def _collect_response(self, run_id: str, provider_request) -> tuple[str, list[ToolCall], Usage]:
        fragments: list[str] = []
        calls: list[ToolCall] = []
        usage = Usage()
        first_token_at: float | None = None
        started = time.monotonic()
        async for delta in self.provider.stream(provider_request):
            run = await self.store.get_run(run_id)
            if run.cancelled:
                raise asyncio.CancelledError()
            if delta.text:
                if first_token_at is None:
                    first_token_at = time.monotonic()
                fragments.append(delta.text)
                await self.emit(run_id, "assistant.delta", {"delta": delta.text})
            calls.extend(delta.tool_calls)
            if delta.usage:
                usage = delta.usage
        usage.ttft_ms = usage.ttft_ms if usage.ttft_ms is not None else (int((first_token_at-started)*1000) if first_token_at else None)
        usage.elapsed_ms = usage.elapsed_ms if usage.elapsed_ms is not None else int((time.monotonic()-started)*1000)
        return "".join(fragments), calls, usage

    async def emit(self, run_id: str, event_type: str, payload: dict) -> None:
        await self.store.append_event(run_id, event_type, payload)
        if event_type.startswith("tool."):
            run = await self.store.get_run(run_id)
            safe_fields = {key: payload[key] for key in ("tool_call_id", "name", "error_code", "elapsed_ms", "ok", "truncated") if key in payload}
            self._log(run, event_type, **safe_fields)

    async def _cancel(self, run_id: str) -> None:
        await self.store.update_run(run_id, status=RunStatus.cancelled, stop_reason="user_cancelled")
        self._log(await self.store.get_run(run_id), "run.cancelled", run_id=run_id, stop_reason="user_cancelled")
        await self.emit(run_id, "run.cancelled", {"stop_reason": "user_cancelled"})

    async def _fail(self, run_id: str, reason: str) -> None:
        await self.store.update_run(run_id, status=RunStatus.failed, stop_reason=reason)
        self._log(await self.store.get_run(run_id), "run.failed", run_id=run_id, stop_reason=reason)
        await self.emit(run_id, "run.failed", {"stop_reason": reason})

    def _log(self, run, event: str, **fields: object) -> None:
        if not self.log_root:
            return
        try:
            append_session_log(self.log_root, run.session_id, f"harness_{run.run_id}", event, fields)
        except OSError:
            # Logging must never break a user-visible Run.
            pass

    def _ensure_workspace(self, run) -> Path | None:
        """The stable location future Agent write tools must use for this Session."""
        if not self.workspace_root:
            return None
        try:
            return session_directory(self.workspace_root, run.workspace_id or run.session_id)
        except OSError:
            return None


def _safe_script_hash(root: Path, value: str) -> str | None:
    try:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            return None
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return __import__("hashlib").sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        return None
