#!/usr/bin/env bash
# Start the local Harness, BFF, and Web UI as one supervised development session.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_ROOT="${LOG_ROOT:-}"
if [[ -z "$LOG_ROOT" && -f "$ROOT_DIR/.env" ]]; then
  LOG_ROOT="$(sed -n 's/^[[:space:]]*LOG_ROOT[[:space:]]*=[[:space:]]*//p' "$ROOT_DIR/.env" | tail -n 1)"
fi
LOG_ROOT="${LOG_ROOT:-.data/logs}"
[[ "$LOG_ROOT" = /* ]] || LOG_ROOT="$ROOT_DIR/$LOG_ROOT"
RUNTIME_DIR="$LOG_ROOT/services"
HOST="${HOST:-127.0.0.1}"
HARNESS_PORT="${HARNESS_PORT:-8001}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.12)"
else
  echo "Python 3.12 is required. Create .venv with Python 3.12, then install: pip install -e '.[dev]'" >&2
  exit 1
fi

require_free_port() {
  local port="$1"
  if "$PYTHON_BIN" - "$port" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0 else 1)
PY
  then
    return
  fi
  echo "Port $port is already in use. Set HARNESS_PORT, BACKEND_PORT, or WEB_PORT to change it." >&2
  exit 1
}

for port in "$HARNESS_PORT" "$BACKEND_PORT" "$WEB_PORT"; do
  require_free_port "$port"
done

if ! "$PYTHON_BIN" -c 'import fastapi, httpx, pydantic, uvicorn' 2>/dev/null; then
  echo "Python dependencies are missing. Run: $PYTHON_BIN -m pip install -e '.[dev]'" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to start the Web UI." >&2
  exit 1
fi
if [[ ! -d "$ROOT_DIR/web/node_modules" ]]; then
  echo "Web dependencies are missing. Run: (cd web && npm install)" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
PIDS=()

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if ((${#PIDS[@]})); then
    echo "Stopping local services…"
    # Each service is started in its own session, so this also terminates
    # descendants such as Vite spawned by npm.
    for pid in "${PIDS[@]}"; do
      kill -TERM -- "-$pid" 2>/dev/null || true
    done
    wait "${PIDS[@]}" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"
echo "Starting Harness on http://$HOST:$HARNESS_PORT"
setsid "$PYTHON_BIN" -m uvicorn harness.app:app --host "$HOST" --port "$HARNESS_PORT" \
  >"$RUNTIME_DIR/harness.log" 2>&1 &
PIDS+=("$!")

echo "Starting Backend on http://$HOST:$BACKEND_PORT"
HARNESS_URL="http://127.0.0.1:$HARNESS_PORT" \
  setsid "$PYTHON_BIN" -m uvicorn backend.app:app --host "$HOST" --port "$BACKEND_PORT" \
  >"$RUNTIME_DIR/backend.log" 2>&1 &
PIDS+=("$!")

echo "Starting Web UI on http://$HOST:$WEB_PORT"
(
  cd "$ROOT_DIR/web"
  exec setsid npm run dev -- --host "$HOST" --port "$WEB_PORT"
) >"$RUNTIME_DIR/web.log" 2>&1 &
PIDS+=("$!")

echo
echo "Services are starting. Open http://$HOST:$WEB_PORT"
echo "Service logs: $RUNTIME_DIR/{harness,backend,web}.log"
echo "Press Ctrl+C to stop all three processes."

if wait -n "${PIDS[@]}"; then
  STATUS=0
else
  STATUS=$?
fi
echo "A service exited (status $STATUS). See $RUNTIME_DIR for logs." >&2
exit "$STATUS"
