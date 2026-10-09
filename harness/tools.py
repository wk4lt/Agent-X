from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from contracts.models import RiskLevel, ToolCall, ToolResult, ToolSpec


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolCatalog:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        self._tools[spec.name] = RegisteredTool(spec, handler)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def active(self, *, allow_writes: bool = False) -> list[RegisteredTool]:
        return [tool for tool in self._tools.values()
                if allow_writes or tool.spec.risk == RiskLevel.read_only]


class ToolPolicy:
    def __init__(self, allow_writes: bool = False) -> None:
        self.allow_writes = allow_writes

    def filter(self, catalog: ToolCatalog) -> list[RegisteredTool]:
        return catalog.active(allow_writes=self.allow_writes)

    def validate(self, tool: RegisteredTool | None, call: ToolCall) -> ToolResult | None:
        if tool is None:
            return ToolResult(ok=False, content="Requested tool is not available.", error_code="tool_not_found")
        if tool.spec.requires_approval or (tool.spec.risk != RiskLevel.read_only and not self.allow_writes):
            return ToolResult(ok=False, content="Tool requires approval.", error_code="approval_required")
        if not isinstance(call.arguments, dict):
            return ToolResult(ok=False, content="Tool arguments must be an object.", error_code="invalid_arguments")
        return None


class ToolExecutor:
    def __init__(self, catalog: ToolCatalog, policy: ToolPolicy, max_concurrency: int = 4) -> None:
        self.catalog, self.policy = catalog, policy
        self._global = asyncio.Semaphore(max_concurrency)
        self._groups: dict[str, asyncio.Semaphore] = {}

    async def execute_batch(self, calls: list[ToolCall], on_event: Callable[..., Awaitable[None]]) -> list[ToolResult]:
        registered = [self.catalog.get(call.name) for call in calls]
        parallel = all(tool and tool.spec.parallel_safe for tool in registered)
        if parallel:
            return list(await asyncio.gather(*[self._execute(call, on_event) for call in calls]))
        return [await self._execute(call, on_event) for call in calls]

    async def _execute(self, call: ToolCall, on_event: Callable[..., Awaitable[None]]) -> ToolResult:
        tool = self.catalog.get(call.name)
        invalid = self.policy.validate(tool, call)
        if invalid:
            await on_event("tool.failed", {"tool_call_id": call.id, "name": call.name, "error_code": invalid.error_code})
            return invalid
        assert tool
        await on_event("tool.started", {"tool_call_id": call.id, "name": call.name})
        group = self._groups.setdefault(tool.spec.concurrency_group or f"tool:{tool.spec.name}",
                                         asyncio.Semaphore(tool.spec.max_concurrency or 1))
        started = time.perf_counter()
        try:
            async with self._global, group:
                result = await asyncio.wait_for(tool.handler(call.arguments), tool.spec.timeout_seconds)
            raw = result.content.encode("utf-8")
            if len(raw) > tool.spec.max_result_bytes:
                result.content = raw[:tool.spec.max_result_bytes].decode("utf-8", errors="ignore")
                result.truncated = True
            await on_event("tool.completed", {"tool_call_id": call.id, "name": call.name,
                                                "ok": result.ok, "elapsed_ms": int((time.perf_counter()-started)*1000),
                                                "truncated": result.truncated})
            return result
        except asyncio.TimeoutError:
            result = ToolResult(ok=False, content="Tool timed out.", error_code="tool_timeout", retryable=tool.spec.idempotent)
        except asyncio.CancelledError:
            await on_event("tool.failed", {"tool_call_id": call.id, "name": call.name, "error_code": "cancelled"})
            raise
        except Exception:
            result = ToolResult(ok=False, content="Tool execution failed.", error_code="tool_execution_error")
        await on_event("tool.failed", {"tool_call_id": call.id, "name": call.name, "error_code": result.error_code,
                                        "elapsed_ms": int((time.perf_counter()-started)*1000)})
        return result


def tool_schema(tool: RegisteredTool) -> dict[str, Any]:
    return {"type": "function", "function": {"name": tool.spec.name, "description": tool.spec.description,
                                                   "parameters": tool.spec.input_schema}}
