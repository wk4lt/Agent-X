"""OpenCode-compatible skill discovery and deliberately separate script execution."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from contracts.models import ToolResult

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TOOL_GROUP = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class SkillSummary(BaseModel):
    name: str
    description: str
    source: str
    trust_level: str
    tool_groups: list[str] = Field(default_factory=list)


class SkillContent(BaseModel):
    name: str
    content: str
    skill_root: str
    content_hash: str
    resource_files: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class SkillRecord:
    summary: SkillSummary
    root: Path
    skill_md: Path
    content_hash: str
    parse_version: str = "opencode-skill-v1"


@dataclass(frozen=True)
class SkillDiagnostic:
    path: str
    error_code: str
    reason: str


@dataclass
class SkillCatalog:
    records: dict[str, SkillRecord]
    diagnostics: list[SkillDiagnostic] = field(default_factory=list)

    def summaries(self) -> list[SkillSummary]:
        return [self.records[name].summary for name in sorted(self.records)]

    def load(self, name: str) -> SkillContent:
        record = self.records.get(name)
        if not record:
            raise KeyError("skill_not_found")
        content = record.skill_md.read_text(encoding="utf-8")
        files = sorted(str(path.relative_to(record.root)) for path in record.root.rglob("*") if path.is_file() and path != record.skill_md)
        return SkillContent(name=name, content=content, skill_root=str(record.root), content_hash=record.content_hash,
                            resource_files=files)


def _worktree_root(path: Path) -> Path:
    for current in (path, *path.parents):
        if (current / ".git").exists():
            return current
    return path


def discover_skills(project_root: Path, global_config_root: Path, *, project_roots: tuple[str, ...],
                    global_roots: tuple[str, ...], trusted_project_names: set[str] | None = None) -> SkillCatalog:
    """Build an immutable snapshot. Project roots win deterministically over global roots."""
    catalog = SkillCatalog(records={})
    sources: list[tuple[Path, str, str]] = []
    root = _worktree_root(project_root.resolve())
    # Walk from worktree to cwd ordering outer first, then nearer roots override within project.
    chain = list(reversed([item for item in (project_root.resolve(), *project_root.resolve().parents) if item.is_relative_to(root)]))
    for base in chain:
        for relative in project_roots:
            sources.append((base / relative, "project", "untrusted"))
    for relative in global_roots:
        sources.append((global_config_root / relative, "global", "trusted"))
    # Later project locations override earlier ones; globals only fill gaps.
    for directory, source, trust in sources:
        if not directory.is_dir():
            continue
        for skill_md in sorted(directory.glob("*/SKILL.md")):
            try:
                effective_trust = "trusted" if skill_md.parent.name in (trusted_project_names or set()) else trust
                record = _parse_skill(skill_md, source, effective_trust)
            except ValueError as exc:
                catalog.diagnostics.append(SkillDiagnostic(str(skill_md), "skill_invalid_manifest", str(exc)))
                continue
            existing = catalog.records.get(record.summary.name)
            if existing:
                if source == "project" and existing.summary.source == "global":
                    catalog.records[record.summary.name] = record
                elif source == existing.summary.source == "project":
                    catalog.diagnostics.append(SkillDiagnostic(str(skill_md), "skill_ambiguous_name", "duplicate project skill; configured root precedence applied"))
                    catalog.records[record.summary.name] = record
                else:
                    catalog.diagnostics.append(SkillDiagnostic(str(skill_md), "skill_ambiguous_name", "project skill takes precedence"))
                continue
            catalog.records[record.summary.name] = record
    return catalog


def _parse_skill(path: Path, source: str, trust: str) -> SkillRecord:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError("frontmatter must start at first line")
    end = raw.find("\n---", 4)
    if end < 0:
        raise ValueError("frontmatter terminator is missing")
    try:
        data = yaml.safe_load(raw[4:end])
    except yaml.YAMLError as exc:
        raise ValueError("invalid YAML frontmatter") from exc
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a mapping")
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or not NAME.fullmatch(name) or not 1 <= len(name) <= 64 or name != path.parent.name:
        raise ValueError("name must match parent directory and OpenCode name rules")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        raise ValueError("description must be between 1 and 1024 characters")
    metadata = data.get("metadata")
    if metadata is not None and (not isinstance(metadata, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())):
        raise ValueError("metadata must be a string-to-string map")
    tool_groups = data.get("tool_groups", [])
    if (not isinstance(tool_groups, list)
            or not all(isinstance(item, str) and TOOL_GROUP.fullmatch(item) for item in tool_groups)):
        raise ValueError("tool_groups must be a list of safe group names")
    tool_groups = list(dict.fromkeys(tool_groups))
    return SkillRecord(SkillSummary(name=name, description=description, source=source, trust_level=trust,
                                    tool_groups=tool_groups), path.parent.resolve(),
                       path.resolve(), hashlib.sha256(raw.encode()).hexdigest())


class SkillScriptRunner:
    def __init__(self, *, workspace_root: Path, python: str, policy: str, allowed_extensions: tuple[str, ...],
                 default_timeout: float, max_timeout: float, max_output_bytes: int, max_concurrent: int) -> None:
        self.workspace_root, self.python, self.policy = workspace_root.resolve(), python, policy
        self.allowed_extensions, self.default_timeout, self.max_timeout = allowed_extensions, default_timeout, max_timeout
        self.max_output_bytes, self._semaphore = max_output_bytes, asyncio.Semaphore(max_concurrent)

    async def run(self, record: SkillRecord, script_path: str, args: list[str], timeout_seconds: float | None) -> ToolResult:
        if self.policy == "deny":
            return ToolResult(ok=False, content="Skill script execution is denied.", error_code="skill_script_approval_required")
        if self.policy == "ask" and record.summary.trust_level != "trusted":
            return ToolResult(ok=False, content="Skill script execution requires approval.", error_code="skill_script_approval_required")
        try:
            script = self._script_path(record.root, script_path)
        except ValueError as exc:
            return ToolResult(ok=False, content=str(exc), error_code="skill_script_path_forbidden")
        if script.suffix not in self.allowed_extensions:
            return ToolResult(ok=False, content="Script runtime is not allowed.", error_code="skill_script_runtime_not_allowed")
        if not isinstance(args, list) or not all(isinstance(arg, str) and "\x00" not in arg for arg in args):
            return ToolResult(ok=False, content="Script arguments must be a string argv array.", error_code="invalid_arguments")
        timeout = min(timeout_seconds or self.default_timeout, self.max_timeout)
        command = [self.python, str(script), *args] if script.suffix == ".py" else ["/bin/bash", str(script), *args]
        started = time.perf_counter()
        async with self._semaphore:
            process = await asyncio.create_subprocess_exec(*command, cwd=str(self.workspace_root), env={"PATH": os.getenv("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
                return ToolResult(ok=False, content="Skill script timed out.", error_code="skill_script_timeout")
            except asyncio.CancelledError:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
                raise
        output = (stdout + stderr)[:self.max_output_bytes].decode("utf-8", errors="replace")
        truncated = len(stdout) + len(stderr) > self.max_output_bytes
        if process.returncode:
            return ToolResult(ok=False, content=output or "Skill script failed.", error_code="skill_script_failed", truncated=truncated)
        return ToolResult(ok=True, content=output, truncated=truncated)

    @staticmethod
    def _script_path(root: Path, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("Skill script path must be a relative path inside the Skill directory.")
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("Skill script was not found inside the Skill directory.")
        return resolved
