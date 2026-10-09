"""Safe, session-scoped filesystem primitives shared by Backend and Harness."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def session_directory(root: Path, session_id: str) -> Path:
    """Create and return exactly `root/session_id`; never allow path traversal."""
    if not _SAFE_ID.fullmatch(session_id):
        raise ValueError("invalid session id")
    resolved_root = root.resolve()
    directory = (resolved_root / session_id).resolve()
    if directory.parent != resolved_root:
        raise ValueError("invalid session directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def append_session_log(log_root: Path, session_id: str, source: str, event: str,
                       fields: dict[str, Any] | None = None) -> None:
    """Append a small, structured diagnostic record without prompt or secret content."""
    if not _SAFE_ID.fullmatch(source):
        raise ValueError("invalid log source")
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **(fields or {})}
    destination = session_directory(log_root, session_id) / f"{source}.jsonl"
    with destination.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
