from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from contracts.models import (
    ContextBuildReport,
    DerivedSummary,
    EventEnvelope,
    RunStatus,
    RunView,
    SessionContextAsset,
    SessionEntry,
    ToolCallStatus,
    Usage,
    new_id,
    utcnow,
)


@dataclass
class RunRecord:
    run_id: str
    task_id: str
    session_id: str
    workspace_id: str | None = None
    status: RunStatus = RunStatus.accepted
    stop_reason: str | None = None
    failure_stage: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    final_answer: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    cancelled: bool = False
    reports: list[ContextBuildReport] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    summary_usages: list[Usage] = field(default_factory=list)
    tool_statuses: dict[str, ToolCallStatus] = field(default_factory=dict)

    def view(self) -> RunView:
        return RunView(
            run_id=self.run_id, task_id=self.task_id, session_id=self.session_id,
            status=self.status, stop_reason=self.stop_reason, failure_stage=self.failure_stage,
            failure_code=self.failure_code, failure_message=self.failure_message,
            final_answer=self.final_answer,
            created_at=self.created_at, updated_at=self.updated_at, workspace_id=self.workspace_id,
        )


class InMemorySessionStore:
    """Replaceable persistence port; intentionally not a process-durability claim."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._sessions: dict[str, list[SessionEntry]] = {}
        self._summaries: dict[str, list[DerivedSummary]] = {}
        self._context_assets: dict[str, dict[tuple[str, str], SessionContextAsset]] = {}
        self._idempotency: dict[str, str] = {}
        self._events: dict[str, list[EventEnvelope]] = {}
        self._changed: dict[str, asyncio.Condition] = {}
        self._session_execution_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, task_id: str, idempotency_key: str, session_id: str | None,
                         workspace_id: str | None = None) -> tuple[RunRecord, bool]:
        async with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing:
                return self._runs[existing], False
            run_id = new_id("run")
            record = RunRecord(run_id=run_id, task_id=task_id, session_id=session_id or new_id("session"), workspace_id=workspace_id)
            self._runs[run_id] = record
            self._sessions.setdefault(record.session_id, [])
            self._summaries.setdefault(record.session_id, [])
            self._context_assets.setdefault(record.session_id, {})
            self._events[run_id] = []
            self._changed[run_id] = asyncio.Condition()
            self._session_execution_locks.setdefault(record.session_id, asyncio.Lock())
            self._idempotency[idempotency_key] = run_id
            return record, True

    def session_execution_lock(self, session_id: str) -> asyncio.Lock:
        """Serialize Runs that append to and build from the same Session."""
        return self._session_execution_locks.setdefault(session_id, asyncio.Lock())

    async def get_run(self, run_id: str) -> RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"Unknown run: {run_id}") from exc

    async def append_entry(self, session_id: str, entry: SessionEntry) -> None:
        self._sessions.setdefault(session_id, []).append(entry)

    async def session_entries(self, session_id: str) -> list[SessionEntry]:
        return list(self._sessions.get(session_id, []))

    async def append_summary(self, session_id: str, summary: DerivedSummary) -> None:
        self._summaries.setdefault(session_id, []).append(summary)

    async def session_summaries(self, session_id: str) -> list[DerivedSummary]:
        return list(self._summaries.get(session_id, []))

    async def put_context_asset(self, session_id: str, asset: SessionContextAsset) -> None:
        async with self._lock:
            assets = self._context_assets.setdefault(session_id, {})
            # One active version per logical asset. Replacing a changed Skill prevents
            # stale instructions from being injected alongside the selected version.
            for key in [key for key in assets if key[0] == asset.kind and key[1] == asset.name]:
                del assets[key]
            assets[(asset.kind, asset.name)] = asset

    async def session_context_assets(self, session_id: str) -> list[SessionContextAsset]:
        assets = self._context_assets.get(session_id, {})
        return [assets[key] for key in sorted(assets)]

    async def update_run(self, run_id: str, *, status: RunStatus | None = None,
                         stop_reason: str | None = None, failure_stage: str | None = None,
                         failure_code: str | None = None, failure_message: str | None = None,
                         final_answer: str | None = None) -> RunRecord:
        record = await self.get_run(run_id)
        if status is not None:
            record.status = status
        if stop_reason is not None:
            record.stop_reason = stop_reason
        if failure_stage is not None:
            record.failure_stage = failure_stage
        if failure_code is not None:
            record.failure_code = failure_code
        if failure_message is not None:
            record.failure_message = failure_message
        if final_answer is not None:
            record.final_answer = final_answer
        record.updated_at = utcnow()
        return record

    async def cancel(self, run_id: str) -> RunRecord:
        record = await self.get_run(run_id)
        record.cancelled = True
        return record

    async def append_event(self, run_id: str, event_type: str, payload: dict) -> EventEnvelope:
        async with self._lock:
            record = await self.get_run(run_id)
            events = self._events[run_id]
            event = EventEnvelope(task_id=record.task_id, run_id=run_id, sequence=len(events) + 1,
                                  type=event_type, payload=payload)
            events.append(event)
        condition = self._changed[run_id]
        async with condition:
            condition.notify_all()
        return event

    async def events_after(self, run_id: str, after_sequence: int = 0) -> list[EventEnvelope]:
        return [item for item in self._events.get(run_id, []) if item.sequence > after_sequence]

    async def wait_for_events(self, run_id: str, after_sequence: int, timeout: float = 15) -> list[EventEnvelope]:
        current = await self.events_after(run_id, after_sequence)
        if current:
            return current
        condition = self._changed[run_id]
        try:
            async with condition:
                await asyncio.wait_for(condition.wait(), timeout)
        except asyncio.TimeoutError:
            return []
        return await self.events_after(run_id, after_sequence)

    async def mark_active_interrupted(self) -> None:
        for record in self._runs.values():
            if record.status in {RunStatus.accepted, RunStatus.running, RunStatus.waiting_for_user}:
                await self.update_run(record.run_id, status=RunStatus.interrupted, stop_reason="worker_interrupted")
                await self.append_event(record.run_id, "run.interrupted", {"stop_reason": "worker_interrupted"})
