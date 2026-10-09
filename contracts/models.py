"""Versioned API and event contracts shared by service boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class RunStatus(str, Enum):
    accepted = "accepted"
    running = "running"
    waiting_for_user = "waiting_for_user"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


TERMINAL_STATUSES = {RunStatus.completed, RunStatus.failed, RunStatus.cancelled, RunStatus.interrupted}


class RiskLevel(str, Enum):
    read_only = "read_only"
    workspace_write = "workspace_write"
    command_exec = "command_exec"
    external_side_effect = "external_side_effect"


class ToolCallStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    outcome_unknown = "outcome_unknown"


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    source: str = "local"
    risk: RiskLevel = RiskLevel.read_only
    parallel_safe: bool = False
    idempotent: bool = False
    concurrency_group: str | None = None
    max_concurrency: int | None = None
    timeout_seconds: float = 30
    max_result_bytes: int = 32_768
    requires_approval: bool = False


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: new_id("call"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    content: str
    artifact_ref: str | None = None
    truncated: bool = False
    error_code: str | None = None
    retryable: bool = False


class SessionEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("entry"))
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class ToolResultReduction(BaseModel):
    entry_id: str
    original_chars: int
    retained_chars: int
    reason: str


class SummaryReference(BaseModel):
    summary_id: str
    source_entry_ids: list[str]
    version: str
    generated_at: datetime
    model: str
    template: str


class DerivedSummary(BaseModel):
    """A derived record. It never replaces SessionEntry history."""
    id: str = Field(default_factory=lambda: new_id("summary"))
    content: str
    source_entry_ids: list[str]
    version: str = "v1"
    generated_at: datetime = Field(default_factory=utcnow)
    model: str
    template: str
    usage: "Usage | None" = None


class ContextBuildReport(BaseModel):
    request_id: str = Field(default_factory=lambda: new_id("req"))
    task_id: str
    run_id: str
    turn_id: str
    model: str
    prompt_version: str = "v1"
    system_chars: int = 0
    system_tokens_est: int = 0
    workflow_skill_chars: int = 0
    workflow_skill_tokens_est: int = 0
    project_context_chars: int = 0
    project_context_tokens_est: int = 0
    history_chars: int = 0
    history_tokens_est: int = 0
    tool_schema_chars: int = 0
    tool_schema_tokens_est: int = 0
    evidence_chars: int = 0
    evidence_tokens_est: int = 0
    tool_result_chars: int = 0
    tool_result_tokens_est: int = 0
    user_input_chars: int = 0
    user_input_tokens_est: int = 0
    input_tokens_est: int = 0
    input_tokens_before_est: int = 0
    input_tokens_after_est: int = 0
    available_input_tokens: int = 0
    output_reserve: int = 1024
    context_window: int = 16_384
    safety_margin: int = 256
    included_entry_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    active_tool_names: list[str] = Field(default_factory=list)
    omitted_items_by_reason: dict[str, list[str]] = Field(default_factory=dict)
    omitted_entry_ids_by_reason: dict[str, list[str]] = Field(default_factory=dict)
    tool_result_reductions: list[ToolResultReduction] = Field(default_factory=list)
    summary_enabled: bool = True
    summary_trigger_reason: str | None = None
    summary_refs: list[SummaryReference] = Field(default_factory=list)
    summary_source_entry_ids: list[str] = Field(default_factory=list)
    summary_usage_actual: Usage | None = None
    compaction_trigger_ratio: float = 0.0
    compaction_target_ratio: float = 0.0


class Usage(BaseModel):
    prompt_tokens_actual: int | None = None
    completion_tokens_actual: int | None = None
    cached_tokens_actual: int | None = None
    ttft_ms: int | None = None
    elapsed_ms: int | None = None
    finish_reason: str | None = None


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    task_id: str
    run_id: str
    sequence: int
    timestamp: datetime = Field(default_factory=utcnow)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunBudget(BaseModel):
    max_turns: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=32, ge=0, le=500)
    wall_time_seconds: float = Field(default=600, gt=0, le=86_400)
    output_reserve: int = Field(default=1024, ge=1)
    context_window: int = Field(default=16_384, ge=1024)
    safety_margin: int = Field(default=256, ge=0)


class CreateRunRequest(BaseModel):
    task_id: str
    idempotency_key: str = Field(min_length=1, max_length=200)
    input: str = Field(min_length=1)
    session_id: str | None = None
    model: str = "mock-agent"
    budget: RunBudget = Field(default_factory=RunBudget)
    selected_skills: list[str] = Field(default_factory=list, max_length=32)
    workspace_id: str | None = None


class RunView(BaseModel):
    run_id: str
    task_id: str
    session_id: str
    status: RunStatus
    stop_reason: str | None = None
    final_answer: str | None = None
    created_at: datetime
    updated_at: datetime
    workspace_id: str | None = None


class CreateTaskRequest(BaseModel):
    input: str = Field(min_length=1)
    project_id: str = "default"
    conversation_id: str | None = None
    selected_skills: list[str] = Field(default_factory=list, max_length=32)


class TaskView(BaseModel):
    task_id: str
    project_id: str
    input: str
    conversation_id: str | None = None
    run: RunView | None = None


class ConversationView(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationPage(BaseModel):
    items: list[ConversationView]
    next_cursor: str | None = None


class ConversationMessageView(BaseModel):
    id: str
    sequence: int
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    run_id: str | None = None
    status: str
    created_at: datetime


class ConversationMessagePage(BaseModel):
    items: list[ConversationMessageView]
    next_cursor: str | None = None


class WorkspaceFile(BaseModel):
    name: str
    size_bytes: int
    modified_at: datetime
