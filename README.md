# TroubleShooter Agent Harness

Fresh V1 implementation based on `Codex_Agent_Harness_Development_Guide.md`. It deliberately
does not reuse prior code. The current milestone provides the phase 0 contracts and phase 1
single-agent core, plus a thin phase 2 BFF and phase 3 React UI shell.

## Design boundaries

- `contracts/`: versioned Task/Run/Event/Tool contracts.
- `harness/`: authoritative Session, Run and event writer; provider/tool ports; no MCP semantics.
- `backend/`: user-owned Task mapping and SSE proxy, communicating with Harness only over HTTP.
- `web/`: Vite/React UI that understands only public API and event envelope.

Harness run state remains process-local in this V1, while browser conversation history, ownership,
messages and Harness run mappings are durable in SQLite through SQLAlchemy Async and Alembic.

## Run locally

Use Python 3.12 (the development shell here has Python 3.10, so it is not the target runtime):

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
uvicorn harness.app:app --port 8001
# separate terminal
HARNESS_URL=http://127.0.0.1:8001 uvicorn backend.app:app --port 8000
# separate terminal
cd web && npm install && npm run dev
```

Or, after installing both Python and Web dependencies once, start all three processes together:

```bash
./dev.sh
```

It starts Harness (`8001`), Backend (`8000`) and the Web UI (`5173`), writes logs to `LOG_ROOT/services/`,
and stops child processes on Ctrl+C. Override ports with `HARNESS_PORT`, `BACKEND_PORT` or
`WEB_PORT`.

Visit Vite's URL. Without credentials the Harness uses `EchoProvider`, which intentionally
returns a safe deterministic response. Copy `.env.example` to `.env` and set the key locally.
`HarnessSettings` validates provider selection, HTTPS base URL, context window, output reserve,
tool-result byte limit, timeouts and concurrency during startup. The adapter streams text and
accumulates partial tool-call arguments before tool execution. Keys are never logged.

## Test

```bash
python -m pytest
```

The test suite uses a scripted fake provider and tool; no model or MCP credentials are required.

## Session workspaces

Every Task/session has a separate local workspace at `.data/workspaces/<session_id>/`. The UI can
upload a file (25 MiB by default), list it and download it again. Configure `WORKSPACE_ROOT` and
`WORKSPACE_MAX_FILE_BYTES` in `.env` when needed. Workspace contents are never automatically
added to model context.

Run diagnostics are separately written as JSONL under `LOG_ROOT/<session_id>/`; service stdout
logs are stored at `LOG_ROOT/services/`. Both paths are configurable in `.env`.

## Durable conversation history

The Backend creates a random anonymous browser identity on first access and stores only a SHA-256
hash of its persistent HttpOnly cookie in SQLite. Conversation and message queries, task control,
SSE and workspace access are all restricted server-side by that identity; no IP address or browser
storage is used for ownership. Cookie settings and the SQLite path are environment-configurable:

```bash
HISTORY_DB_PATH=.data/agent_history.db
HISTORY_BUSY_TIMEOUT_MS=5000
ANON_COOKIE_SECURE=false # true for HTTPS deployment
```

The database parent directory is created at startup, SQLite runs with foreign keys, WAL and the
configured busy timeout, and Alembic upgrades it automatically during Backend startup. The schema
revision lives in `backend/migrations/`; inspect or run it explicitly with
`python -m backend.migrate`. SQLite is intentionally for a single Backend instance. Move the same
Repository/API contract to PostgreSQL before multi-instance or high-write-concurrency deployment.

## Skills

The Harness discovers OpenCode-compatible `SKILL.md` files at Run start from `.opencode/skills`,
`.claude/skills` and `.agents/skills` beneath the project worktree, then from the corresponding
global configuration locations. Project skills override global ones. The model sees only the name
and description until it calls `load_skill`; loading instructions never executes their scripts.

`run_skill_script` is a separate, argv-only tool. It accepts only a discovered Skill name and a
relative file below that Skill root, allows `.py` and `.sh` by default, uses a restricted environment,
and applies timeout, output and concurrency limits. Project scripts default to `ask` through
`SKILL_SCRIPT_POLICY`; set an explicit trusted/allow deployment policy only after reviewing code.
