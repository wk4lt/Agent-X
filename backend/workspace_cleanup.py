"""Retention cleanup for per-conversation workspace directories."""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path


def remove_workspace_directories(root: Path, conversation_ids: list[str]) -> list[str]:
    """Remove only direct, non-symlink workspace directories selected by history retention."""
    resolved_root = root.resolve()
    removed: list[str] = []
    for conversation_id in conversation_ids:
        directory = resolved_root / conversation_id
        try:
            if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != resolved_root:
                continue
            shutil.rmtree(directory)
        except OSError:
            continue
        removed.append(conversation_id)
    return removed


async def prune_expired_workspaces(history, root: Path, retention_days: int,
                                   *, now: datetime | None = None) -> list[str]:
    """Delete workspace files for conversations inactive beyond the configured retention period."""
    if retention_days == 0:
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    conversation_ids = await history.expired_workspace_conversation_ids(cutoff)
    return await asyncio.to_thread(remove_workspace_directories, root, conversation_ids)
