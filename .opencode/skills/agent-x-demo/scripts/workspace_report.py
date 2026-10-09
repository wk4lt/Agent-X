"""Safe, read-only demo script for the Agent-X Skill runner."""
from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    relative = sys.argv[1] if len(sys.argv) > 1 else "."
    root = Path(relative).resolve()
    if not root.is_dir():
        print(json.dumps({"ok": False, "error": "directory_not_found"}))
        return 2
    files = []
    excluded = {".git", ".data", ".env", ".venv", ".pytest_cache", ".run", "node_modules", "dist"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in excluded or part == "__pycache__" or part.endswith(".egg-info") for part in path.parts):
            files.append({"name": str(path.relative_to(root)), "size_bytes": path.stat().st_size})
    print(json.dumps({"ok": True, "root": str(root), "file_count": len(files), "files": files[:100]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
