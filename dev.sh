#!/usr/bin/env bash
# Linux development service lifecycle; see scripts/dev_services.py.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.12)"
else
  echo "Python 3.12 is required. Create .venv, then install: pip install -e '.[dev]'" >&2
  exit 1
fi
exec "$PYTHON_BIN" "$ROOT_DIR/scripts/dev_services.py" "$@"
