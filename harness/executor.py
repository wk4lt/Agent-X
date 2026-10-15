from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path

from contracts.models import (
    CreateRunRequest, RunStatus, SessionContextAsset, SessionEntry, ToolCall, ToolResult,
    ToolSpec, Usage, new_id,
)
from contracts.session_storage import append_session_log, session_directory
from harness.context import ContextBudgetExceeded, ContextBuilder
from harness.mcp_tools import McpToolRegistry
from harness.provider import ProviderAdapter
from harness.skills import SkillScriptRunner, discover_skills
from harness.store import InMemorySessionStore
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy


LOG = logging.getLogger("agent_x.harness")


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
    mcp_registry: McpToolRegistry | None = None

    async def execute(self, run_id: str, request: CreateRunRequest) -> None:
        try:
            run = await self.store.get_run(run_id)
            async with self.store.session_execution_lock(run.session_id):
                await self._execute(run_id, request)
        except asyncio.CancelledError:
            if (await self.store.get_run(run_id)).status is not RunStatus.cancelled:
                await self._cancel(run_id)
            raise
        except Exception as exc:
            await self._fail(run_id, "runtime_error", stage="initialization", exception=exc)

    async def _execute(self, run_id: str, request: CreateRunRequest) -> None:
        run = await self.store.get_run(run_id)
        started = time.monotonic()
        run_workspace = self._ensure_workspace(run)
        await self.store.update_run(run_id, status=RunStatus.running)
        self._log(run, "run.started", task_id=run.task_id, run_id=run_id, model=request.model)
        await self.emit(run_id, "run.started", {})
        await self.store.append_entry(run.session_id, SessionEntry(role="user", content=request.input))
        settings = getattr(self, "settings", None)
        run_tools = self.tool_executor
        run_context = self.context_builder
        if run_workspace is not None:
            workspace_instructions = (
                "Current user workspace (trusted runtime metadata): " + str(run_workspace) + "\n"
                "Use this directory as the base location for every user-file read, write, and generated artifact. "
                "Do not read or write user files outside this directory. Skill definitions and bundled Skill "
                "resources are read-only instructions/resources; do not use the Skill directory as the user workspace. "
                "Skill scripts run with this directory as cwd and receive the same path in AGENT_X_WORKSPACE."
            )
            run_context = replace(
                run_context,
                task_constraints="\n".join(filter(None, [run_context.task_constraints, workspace_instructions])),
            )
        base_task_constraints = run_context.task_constraints
        if settings is not None:
            if not request.selected_skills:
                await self._complete(run_id, "请先选择与问题相关的 Skill，再提交问题。", "skill_selection_required")
                return
            if settings.skill_load_policy == "deny":
                await self._complete(run_id, "当前 Skill 功能已禁用，暂时无法处理问题。", "skill_load_denied")
                return
            snapshot = discover_skills(settings.skill_project_root)
            selected = set(request.selected_skills)
            if selected - snapshot.records.keys():
                await self._complete(run_id, "所选 Skill 不可用，请重新选择后提交问题。", "skill_selection_invalid")
                return
            snapshot.records = {name: record for name, record in snapshot.records.items() if name in selected}
            selected_assets = {
                asset.name: asset
                for asset in await self.store.session_context_assets(run.session_id)
                if asset.kind == "skill" and asset.name in selected
                and asset.content_hash == snapshot.records[asset.name].content_hash
            }

            def refresh_skill_context() -> None:
                nonlocal run_context
                rendered = [
                    f"Selected skill instructions (name={name}, sha256={asset.content_hash}):\n{asset.content}"
                    for name, asset in sorted(selected_assets.items())
                ]
                run_context = replace(
                    run_context,
                    task_constraints="\n\n".join(filter(None, [base_task_constraints, *rendered])),
                )

            refresh_skill_context()
            scope = json.dumps([{"name": item.name, "description": item.description}
                                for item in snapshot.summaries()], ensure_ascii=False)
            # Keep scope local to this Run: the builder is shared by concurrent executions.
            run_context = replace(
                run_context,
                system_rules=run_context.system_rules + f"\nSelected skills (JSON data): {scope}",
            )
            for summary in snapshot.summaries():
                await self.emit(run_id, "skill.discovered", {"name": summary.name, "source": summary.source,
                    "trust_level": summary.trust_level, "content_hash": snapshot.records[summary.name].content_hash})
            for diagnostic in snapshot.diagnostics:
                await self.emit(run_id, "skill.invalid", {"error_code": diagnostic.error_code, "path": diagnostic.path})
            catalog = ToolCatalog()
            for registered in self.tool_executor.catalog.active(allow_writes=True):
                catalog.register(registered.spec, registered.handler)
            if self.mcp_registry is not None and request.selected_skills:
                groups = {
                    group
                    for record in snapshot.records.values()
                    for group in record.summary.tool_groups
                }
                selection = await self.mcp_registry.tools_for_groups(groups)
                for diagnostic in selection.diagnostics:
                    await self.emit(run_id, "mcp.server.failed", {
                        "server": diagnostic.server,
                        "error_code": diagnostic.error_code,
                    })
                mounted = []
                for registered in selection.tools:
                    try:
                        catalog.register(registered.spec, registered.handler)
                    except ValueError:
                        await self.emit(run_id, "mcp.tool.failed", {
                            "server": registered.spec.source.removeprefix("mcp:"),
                            "name": registered.spec.name,
                            "error_code": "mcp_tool_name_collision",
                        })
                        continue
                    mounted.append(registered)
                if selection.servers:
                    await self.emit(run_id, "mcp.tools.mounted", {
                        "servers": list(selection.servers),
                        "tool_names": [tool.spec.name for tool in mounted],
                        "tool_count": len(mounted),
                    })
            loaded_hashes = {asset.content_hash for asset in selected_assets.values()}
            runner = SkillScriptRunner(workspace_root=run_workspace or settings.skill_project_root, python=os.getenv("PYTHON_EXECUTABLE", sys.executable),
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
                    return ToolResult(ok=True, content="Skill instructions are already loaded in session context.")
                loaded_hashes.add(content.content_hash)
                asset = SessionContextAsset(
                    name=name, content_hash=content.content_hash, content=content.content,
                )
                await self.store.put_context_asset(run.session_id, asset)
                selected_assets[name] = asset
                refresh_skill_context()
                await self.emit(run_id, "skill.loaded", {"name": name, "source": snapshot.records[name].summary.source,
                    "content_hash": content.content_hash})
                return ToolResult(
                    ok=True,
                    content=f"Skill {name!r} was loaded into stable session context (sha256={content.content_hash}).",
                )

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
            catalog.register(ToolSpec(name="run_skill_script", description="Execute an allowed script inside a loaded/discovered Skill using argv arguments. The current user workspace is the process cwd and is also available as AGENT_X_WORKSPACE.",
                input_schema={"type": "object", "properties": {"skill_name": {"type": "string"}, "script_path": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}, "timeout_seconds": {"type": "number"}}, "required": ["skill_name", "script_path", "args"]}), run_skill_script)
            run_tools = ToolExecutor(catalog, self.policy, settings.tool_max_concurrency)
        tool_calls_used = 0
        pending_calls: list[ToolCall] = []
        stage = "run_execution"
        turn_id: str | None = None
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
                    stage = "context_build"
                    await self.emit(run_id, "run.progress", {
                        "round": turn_number, "phase": "context", "message": "正在构建上下文",
                    })
                    async def persist_summary(summary):
                        await self.store.append_summary(run.session_id, summary)
                        if summary.usage:
                            run.summary_usages.append(summary.usage)
                            await self.emit(run_id, "usage.updated", {"kind": "summary", **summary.usage.model_dump(exclude_none=True)})

                    async def compaction_event(event_type: str, payload: dict) -> None:
                        await self.emit(run_id, event_type, {"round": turn_number, **payload})

                    provider_request, report = await run_context.build(
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
                stage = "provider_stream"
                await self.emit(run_id, "run.progress", {
                    "round": turn_number, "phase": "model", "message": "正在等待模型响应",
                })
                text, calls, usage = await self._collect_response(run_id, provider_request, turn_number)
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
                    pending_calls = list(calls)
                    stage = "tool_execution"
                    await self.emit(run_id, "run.progress", {
                        "round": turn_number, "phase": "tools",
                        "message": f"正在执行 {len(calls)} 个工具调用",
                    })
                    results = await run_tools.execute_batch(
                        calls,
                        lambda typ, payload: self.emit(run_id, typ, {"round": turn_number, **payload}),
                    )
                    for call, result in zip(calls, results, strict=True):
                        await self.store.append_entry(run.session_id, SessionEntry(
                            role="tool", content=result.model_dump_json(), tool_call_id=call.id, tool_name=call.name))
                    pending_calls = []
                    continue
                stage = "completion"
                await self._complete(run_id, text)
                return
            await self._fail(run_id, "max_turns")
        except asyncio.CancelledError:
            await self._repair_pending_tool_calls(run.session_id, pending_calls)
            await self._cancel(run_id)
            raise
        except Exception as exc:
            await self._repair_pending_tool_calls(run.session_id, pending_calls)
            await self._fail(run_id, "provider_error", stage=stage, turn_id=turn_id, exception=exc)

    async def _repair_pending_tool_calls(self, session_id: str, calls: list[ToolCall]) -> None:
        if not calls:
            return
        completed = {
            entry.tool_call_id
            for entry in await self.store.session_entries(session_id)
            if entry.role == "tool" and entry.tool_call_id
        }
        for call in calls:
            if call.id in completed:
                continue
            result = ToolResult(
                ok=False,
                content="Tool execution was interrupted; its external outcome may be unknown.",
                error_code="tool_interrupted",
            )
            await self.store.append_entry(session_id, SessionEntry(
                role="tool", content=result.model_dump_json(),
                tool_call_id=call.id, tool_name=call.name,
            ))

    async def _collect_response(self, run_id: str, provider_request, turn_number: int) -> tuple[str, list[ToolCall], Usage]:
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
                    await self.emit(run_id, "run.progress", {
                        "round": turn_number, "phase": "answer", "message": "正在生成回答",
                    })
                fragments.append(delta.text)
                await self.emit(run_id, "assistant.delta", {"delta": delta.text, "round": turn_number})
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

    async def _complete(self, run_id: str, text: str, reason: str = "final_answer") -> None:
        run = await self.store.get_run(run_id)
        await self.store.append_entry(run.session_id, SessionEntry(role="assistant", content=text))
        await self.store.update_run(run_id, status=RunStatus.completed, stop_reason=reason, final_answer=text)
        self._log(run, "run.completed", run_id=run_id, stop_reason=reason)
        await self.emit(run_id, "assistant.message", {"content": text})
        await self.emit(run_id, "run.completed", {"stop_reason": reason})

    async def _cancel(self, run_id: str) -> None:
        await self.store.update_run(run_id, status=RunStatus.cancelled, stop_reason="user_cancelled")
        self._log(await self.store.get_run(run_id), "run.cancelled", run_id=run_id, stop_reason="user_cancelled")
        await self.emit(run_id, "run.cancelled", {"stop_reason": "user_cancelled"})

    async def _fail(self, run_id: str, reason: str, *, stage: str | None = None, turn_id: str | None = None,
                    exception: Exception | None = None) -> None:
        details = _exception_details(exception) if exception else {}
        await self.store.update_run(
            run_id, status=RunStatus.failed, stop_reason=reason, failure_stage=stage,
            failure_code=details.get("error_code"), failure_message=details.get("error_message"),
        )
        run = await self.store.get_run(run_id)
        self._log(run, "run.failed", run_id=run_id, stop_reason=reason, stage=stage, turn_id=turn_id, **details)
        if exception:
            LOG.error("run failed task_id=%s run_id=%s session_id=%s stop_reason=%s stage=%s turn_id=%s error_code=%s error_message=%s\n%s",
                      run.task_id, run_id, run.session_id, reason, stage, turn_id,
                      details["error_code"], details["error_message"], details["traceback"])
        else:
            LOG.warning("run failed task_id=%s run_id=%s session_id=%s stop_reason=%s stage=%s turn_id=%s",
                        run.task_id, run_id, run.session_id, reason, stage, turn_id)
        await self.emit(run_id, "run.failed", {"stop_reason": reason, "stage": stage, "turn_id": turn_id,
                                                 **{key: value for key, value in details.items() if key != "traceback"}})

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


def _exception_details(exc: Exception) -> dict[str, str]:
    """Keep a useful local traceback while keeping known credential formats out of logs/events."""
    message = _redact_diagnostic(str(exc), limit=1_200) or "unexpected runtime error"
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {
        "error_code": type(exc).__name__,
        "error_message": message,
        "traceback": _redact_diagnostic(formatted, limit=8_000),
    }


def _redact_diagnostic(value: str, *, limit: int) -> str:
    cleaned = re.sub(r"(?i)\\b(api[_-]?key|authorization|token|password|secret)\\b(\\s*[=:]\\s*)([^\\s,;]+)",
                     r"\\1\\2[REDACTED]", value)
    return cleaned[:limit]
