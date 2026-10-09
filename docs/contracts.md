# V1 contract

The services exchange only the Pydantic models in `contracts/models.py`.  Event envelopes are
schema version 1 and are append-only per run: `sequence` starts at 1 and is never reused.

Internal Harness endpoints:

- `POST /internal/runs` (requires `X-Internal-Token` when configured)
- `GET /internal/runs/{run_id}`
- `GET /internal/runs/{run_id}/session` (Backend-only structured history sync)
- `POST /internal/runs/{run_id}/cancel`
- `GET /internal/runs/{run_id}/events?after_sequence=N`
- `POST /internal/runs/{run_id}/resume`

Public Backend endpoints include `/api/conversations`, conversation message pagination and title/delete
operations, `/api/tasks`, `/api/tasks/{task_id}`, task SSE, cancel and resume. Backend request creation
accepts `conversation_id` and an optional `Idempotency-Key`; it validates the conversation ownership
before creating a Harness Run. A task/run/SSE/workspace lookup likewise begins with an ownership-scoped
ConversationRun lookup and returns 404 for either missing or unauthorized identifiers.
The Backend owns Tasks; Harness owns Sessions, Runs, events, tool calls, reports and usage.
`resume` is an explicit user retry which creates a fresh Run and Session; it never silently
replays an uncertain tool side effect in the old Run.

`context.built` exposes only safe status data: estimated token counts before/after trimming,
available input budget, omission counts, tool-result reduction counts and summary reference IDs.
`usage.updated` has a `kind` of `run` or `summary`; provider token values are actual values, while
the context event values are explicitly estimates. `context.compaction.started`,
`context.compaction.completed` and `context.compaction.failed` are replayable Run events.

Conversation messages are stored in increasing `sequence` order. They preserve assistant `tool_calls`
and tool `tool_call_id`/`tool_name` fields rather than flattening tools into display text. The backend
persists the complete Harness Session entries before forwarding `assistant.message` or `run.completed`
over SSE; repeated reconnects are de-duplicated by Harness entry ID.

Skills are discovered at Run start from OpenCode-compatible directories. `list_skills` publishes
only names/descriptions; `load_skill` is separate and never executes resources. `run_skill_script`
accepts only `{skill_name, script_path, args, timeout_seconds}` and is subject to its own policy.
Skill event types include `skill.discovered`, `skill.loaded`, `skill.invalid`,
`skill.script.approval_required`, `skill.script.started`, `skill.script.completed`, and
`skill.script.failed`; none include Skill bodies, environment values, or unbounded output.
