"""MCP client integration: configuration, discovery, and ToolSpec adaptation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncContextManager, Protocol
from urllib.parse import urlparse

import yaml

from contracts.models import RiskLevel, ToolResult, ToolSpec
from harness.tools import RegisteredTool


SERVER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
GROUP_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class McpConfigurationError(ValueError):
    pass


class McpClientProtocol(Protocol):
    async def list_tools(self, *, cursor: str | None = None) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


McpClientFactory = Callable[["McpServerConfig"], AsyncContextManager[McpClientProtocol]]


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str
    tool_groups: tuple[str, ...]
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    url: str | None = None
    env_from: Mapping[str, str] = field(default_factory=dict)
    headers_from_env: Mapping[str, str] = field(default_factory=dict)
    allowed_tools: frozenset[str] | None = None
    risk: RiskLevel = RiskLevel.read_only
    parallel_safe: bool = False
    idempotent: bool = False
    max_concurrency: int = 1
    timeout_seconds: float = 30.0
    max_result_bytes: int = 32_768
    requires_approval: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class McpServerSummary:
    name: str
    transport: str
    tool_groups: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class McpDiagnostic:
    server: str
    error_code: str
    reason: str


@dataclass(frozen=True)
class McpToolSelection:
    tools: tuple[RegisteredTool, ...]
    diagnostics: tuple[McpDiagnostic, ...]
    servers: tuple[str, ...]


@dataclass(frozen=True)
class _DiscoveredTool:
    name: str
    title: str | None
    description: str | None
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class _CacheEntry:
    discovered_at: float
    tools: tuple[_DiscoveredTool, ...]


def load_mcp_servers(
    path: Path,
    *,
    default_timeout_seconds: float = 30.0,
    default_max_result_bytes: int = 32_768,
) -> tuple[McpServerConfig, ...]:
    """Load a versioned config. A missing file means that MCP is disabled."""
    if not path.is_file():
        return ()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise McpConfigurationError(f"Could not read MCP config: {path}") from exc
    if raw is None:
        return ()
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise McpConfigurationError("MCP config must be a mapping with version: 1")
    if not all(isinstance(key, str) for key in raw):
        raise McpConfigurationError("MCP config field names must be strings")
    unknown = sorted(set(raw) - {"version", "servers"})
    if unknown:
        raise McpConfigurationError(f"MCP config has unknown fields: {', '.join(unknown)}")
    servers = raw.get("servers", {})
    if not isinstance(servers, dict):
        raise McpConfigurationError("MCP config servers must be a mapping")
    return tuple(
        _parse_server(name, value, path.parent, default_timeout_seconds, default_max_result_bytes)
        for name, value in servers.items()
    )


def _parse_server(
    name: Any,
    raw: Any,
    config_root: Path,
    default_timeout_seconds: float,
    default_max_result_bytes: int,
) -> McpServerConfig:
    if not isinstance(name, str) or not SERVER_NAME.fullmatch(name):
        raise McpConfigurationError("MCP server names must match [a-z][a-z0-9_-]{0,31}")
    if not isinstance(raw, dict):
        raise McpConfigurationError(f"MCP server {name!r} must be a mapping")
    if not all(isinstance(key, str) for key in raw):
        raise McpConfigurationError(f"MCP server {name!r} field names must be strings")
    transport = raw.get("transport")
    if transport not in {"stdio", "streamable_http"}:
        raise McpConfigurationError(f"MCP server {name!r} has an unsupported transport")
    common_keys = {
        "transport", "tool_groups", "allowed_tools", "risk", "parallel_safe", "idempotent",
        "max_concurrency", "timeout_seconds", "max_result_bytes", "requires_approval", "enabled",
    }
    transport_keys = (
        {"command", "args", "cwd", "env_from"}
        if transport == "stdio"
        else {"url", "headers_from_env"}
    )
    unknown = sorted(set(raw) - common_keys - transport_keys)
    if unknown:
        raise McpConfigurationError(
            f"MCP server {name!r} has unknown or forbidden fields: {', '.join(unknown)}"
        )
    groups = _string_list(raw.get("tool_groups"), f"MCP server {name!r} tool_groups")
    if not groups or any(not GROUP_NAME.fullmatch(group) for group in groups):
        raise McpConfigurationError(f"MCP server {name!r} needs valid tool_groups")
    command = raw.get("command")
    args = _string_list(raw.get("args", []), f"MCP server {name!r} args")
    url = raw.get("url")
    cwd_value = raw.get("cwd")
    cwd: Path | None = None
    if transport == "stdio":
        if not isinstance(command, str) or not command.strip():
            raise McpConfigurationError(f"MCP stdio server {name!r} needs command")
        if url is not None:
            raise McpConfigurationError(f"MCP stdio server {name!r} cannot set url")
        if cwd_value is not None:
            if not isinstance(cwd_value, str) or not cwd_value:
                raise McpConfigurationError(f"MCP server {name!r} cwd must be a path")
            candidate = Path(cwd_value).expanduser()
            cwd = (
                (config_root / candidate).resolve()
                if not candidate.is_absolute()
                else candidate.resolve()
            )
    else:
        if command is not None or args or cwd_value is not None:
            raise McpConfigurationError(
                f"MCP HTTP server {name!r} cannot set command, args, or cwd"
            )
        if not isinstance(url, str):
            raise McpConfigurationError(f"MCP HTTP server {name!r} needs url")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise McpConfigurationError(f"MCP server {name!r} url must be HTTP(S)")
        if parsed.username or parsed.password:
            raise McpConfigurationError(f"MCP server {name!r} url cannot contain credentials")
    env_from = _string_map(raw.get("env_from", {}), f"MCP server {name!r} env_from")
    if any(
        not ENV_NAME.fullmatch(key) or not ENV_NAME.fullmatch(value)
        for key, value in env_from.items()
    ):
        raise McpConfigurationError(
            f"MCP server {name!r} env_from contains an invalid variable name"
        )
    headers_from_env = _string_map(
        raw.get("headers_from_env", {}), f"MCP server {name!r} headers_from_env"
    )
    if any(not ENV_NAME.fullmatch(value) for value in headers_from_env.values()):
        raise McpConfigurationError(
            f"MCP server {name!r} headers_from_env contains an invalid variable name"
        )
    allowed_raw = raw.get("allowed_tools")
    allowed_tools = None if allowed_raw is None else frozenset(
        _string_list(allowed_raw, f"MCP server {name!r} allowed_tools")
    )
    if "risk" not in raw:
        raise McpConfigurationError(f"MCP server {name!r} must declare risk explicitly")
    try:
        risk = RiskLevel(raw["risk"])
    except ValueError as exc:
        raise McpConfigurationError(f"MCP server {name!r} has an invalid risk") from exc
    max_concurrency = _positive_int(
        raw.get("max_concurrency", 1), f"MCP server {name!r} max_concurrency"
    )
    timeout = _positive_number(
        raw.get("timeout_seconds", default_timeout_seconds),
        f"MCP server {name!r} timeout_seconds",
    )
    max_bytes = _positive_int(
        raw.get("max_result_bytes", default_max_result_bytes),
        f"MCP server {name!r} max_result_bytes",
    )
    for key in ("enabled", "parallel_safe", "idempotent", "requires_approval"):
        if key in raw and not isinstance(raw[key], bool):
            raise McpConfigurationError(f"MCP server {name!r} {key} must be a boolean")
    return McpServerConfig(
        name=name,
        transport=transport,
        tool_groups=tuple(dict.fromkeys(groups)),
        command=command,
        args=tuple(args),
        cwd=cwd,
        url=url,
        env_from=env_from,
        headers_from_env=headers_from_env,
        allowed_tools=allowed_tools,
        risk=risk,
        parallel_safe=raw.get("parallel_safe", False),
        idempotent=raw.get("idempotent", False),
        max_concurrency=max_concurrency,
        timeout_seconds=timeout,
        max_result_bytes=max_bytes,
        requires_approval=raw.get("requires_approval", False),
        enabled=raw.get("enabled", True),
    )


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise McpConfigurationError(f"{label} must be a list of non-empty strings")
    return value


def _string_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and key and isinstance(item, str) and item for key, item in value.items()
    ):
        raise McpConfigurationError(f"{label} must be a string map")
    return dict(value)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise McpConfigurationError(f"{label} must be a positive integer")
    return value


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise McpConfigurationError(f"{label} must be a positive number")
    return float(value)


@asynccontextmanager
async def sdk_mcp_client(server: McpServerConfig):
    """Create an official MCP SDK v2 client without exposing secrets to Agent-X state."""
    try:
        from mcp import Client, StdioServerParameters
    except ImportError as exc:  # pragma: no cover - exercised by deployment, not fake-client tests
        raise RuntimeError("Install the 'mcp>=2,<3' package to use MCP servers") from exc

    if server.transport == "stdio":
        env = _resolve_environment(server.env_from)
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env=env or None,
            cwd=server.cwd,
        )
        async with Client(params) as client:
            yield client
        return

    headers = _resolve_environment(server.headers_from_env)
    if not headers:
        async with Client(server.url) as client:
            yield client
        return

    try:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:  # pragma: no cover - installed transitively with MCP SDK v2
        raise RuntimeError(
            "MCP HTTP header support requires the MCP SDK v2 HTTP dependencies"
        ) from exc
    async with httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(30.0, read=max(300.0, server.timeout_seconds)),
    ) as http_client:
        transport = streamable_http_client(server.url, http_client=http_client)
        async with Client(transport) as client:
            yield client


def _resolve_environment(mapping: Mapping[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for target, source in mapping.items():
        value = os.getenv(source)
        if value is None:
            missing.append(source)
        else:
            resolved[target] = value
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(f"Required MCP environment variables are missing: {names}")
    return resolved


class McpToolRegistry:
    def __init__(
        self,
        servers: tuple[McpServerConfig, ...],
        *,
        client_factory: McpClientFactory = sdk_mcp_client,
        discovery_timeout_seconds: float = 15.0,
        schema_cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._servers = servers
        self._client_factory = client_factory
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._schema_cache_ttl_seconds = schema_cache_ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._locks = {server.name: asyncio.Lock() for server in servers}

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        default_timeout_seconds: float,
        default_max_result_bytes: int,
        discovery_timeout_seconds: float,
        schema_cache_ttl_seconds: float,
        client_factory: McpClientFactory = sdk_mcp_client,
    ) -> "McpToolRegistry":
        return cls(
            load_mcp_servers(
                path,
                default_timeout_seconds=default_timeout_seconds,
                default_max_result_bytes=default_max_result_bytes,
            ),
            client_factory=client_factory,
            discovery_timeout_seconds=discovery_timeout_seconds,
            schema_cache_ttl_seconds=schema_cache_ttl_seconds,
        )

    def summaries(self) -> tuple[McpServerSummary, ...]:
        return tuple(
            McpServerSummary(server.name, server.transport, server.tool_groups, server.enabled)
            for server in self._servers
        )

    async def tools_for_groups(self, groups: set[str]) -> McpToolSelection:
        selected = tuple(
            server for server in self._servers
            if server.enabled and groups.intersection(server.tool_groups)
        )
        if not selected:
            return McpToolSelection((), (), ())
        discoveries = await asyncio.gather(*(self._safe_discover(server) for server in selected))
        tools: list[RegisteredTool] = []
        diagnostics: list[McpDiagnostic] = []
        connected_servers: list[str] = []
        exposed_names: set[str] = set()
        for server, (discovered, diagnostic) in zip(selected, discoveries, strict=True):
            if diagnostic:
                diagnostics.append(diagnostic)
                continue
            connected_servers.append(server.name)
            for remote in discovered:
                if server.allowed_tools is not None and remote.name not in server.allowed_tools:
                    continue
                exposed_name = _exposed_tool_name(server.name, remote.name)
                if exposed_name in exposed_names:
                    diagnostics.append(
                        McpDiagnostic(server.name, "mcp_tool_name_collision", exposed_name)
                    )
                    continue
                exposed_names.add(exposed_name)
                spec = ToolSpec(
                    name=exposed_name,
                    description=_tool_description(server, remote),
                    input_schema=remote.input_schema,
                    source=f"mcp:{server.name}",
                    risk=server.risk,
                    parallel_safe=server.parallel_safe,
                    idempotent=server.idempotent,
                    concurrency_group=f"mcp:{server.name}",
                    max_concurrency=server.max_concurrency,
                    timeout_seconds=server.timeout_seconds,
                    max_result_bytes=server.max_result_bytes,
                    requires_approval=server.requires_approval,
                )

                async def handler(
                    arguments: dict[str, Any], *, cfg=server, tool_name=remote.name
                ) -> ToolResult:
                    return await self._call(cfg, tool_name, arguments)

                tools.append(RegisteredTool(spec, handler))
        return McpToolSelection(tuple(tools), tuple(diagnostics), tuple(connected_servers))

    async def _safe_discover(
        self, server: McpServerConfig
    ) -> tuple[tuple[_DiscoveredTool, ...], McpDiagnostic | None]:
        try:
            return await self._discover(server), None
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return (), McpDiagnostic(
                server.name, "mcp_discovery_timeout", "MCP discovery timed out"
            )
        except Exception as exc:
            return (), McpDiagnostic(server.name, "mcp_server_unavailable", type(exc).__name__)

    async def _discover(self, server: McpServerConfig) -> tuple[_DiscoveredTool, ...]:
        cached = self._cache.get(server.name)
        now = time.monotonic()
        if cached and now - cached.discovered_at < self._schema_cache_ttl_seconds:
            return cached.tools
        async with self._locks[server.name]:
            cached = self._cache.get(server.name)
            now = time.monotonic()
            if cached and now - cached.discovered_at < self._schema_cache_ttl_seconds:
                return cached.tools
            tools = await asyncio.wait_for(
                self._discover_uncached(server), timeout=self._discovery_timeout_seconds
            )
            self._cache[server.name] = _CacheEntry(time.monotonic(), tools)
            return tools

    async def _discover_uncached(self, server: McpServerConfig) -> tuple[_DiscoveredTool, ...]:
        discovered: list[_DiscoveredTool] = []
        cursor: str | None = None
        pages = 0
        async with self._client_factory(server) as client:
            while True:
                pages += 1
                if pages > 100:
                    raise RuntimeError("MCP tool pagination exceeded 100 pages")
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    schema = tool.input_schema
                    if not isinstance(schema, dict):
                        raise TypeError("MCP tool input_schema must be an object")
                    discovered.append(
                        _DiscoveredTool(tool.name, tool.title, tool.description, schema)
                    )
                cursor = getattr(page, "next_cursor", None)
                if cursor is None:
                    break
        return tuple(discovered)

    async def _call(
        self, server: McpServerConfig, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            async with self._client_factory(server) as client:
                result = await client.call_tool(tool_name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult(
                ok=False,
                content=f"MCP server {server.name!r} is unavailable ({type(exc).__name__}).",
                error_code="mcp_server_unavailable",
                retryable=server.idempotent,
            )
        content = _render_result(result)
        if getattr(result, "is_error", False):
            return ToolResult(
                ok=False, content=content or "MCP tool failed.", error_code="mcp_tool_error"
            )
        return ToolResult(ok=True, content=content)


def _exposed_tool_name(server_name: str, remote_name: str) -> str:
    safe_remote = re.sub(r"[^A-Za-z0-9_-]", "_", remote_name).strip("_") or "tool"
    value = f"mcp__{server_name}__{safe_remote}"
    if len(value) <= 64:
        return value
    digest = hashlib.sha256(remote_name.encode()).hexdigest()[:8]
    return f"{value[:55]}_{digest}"


def _tool_description(server: McpServerConfig, tool: _DiscoveredTool) -> str:
    description = tool.description or tool.title or f"MCP tool {tool.name}"
    return f"[{server.name} MCP] {description}"


def _render_result(result: Any) -> str:
    blocks: list[str] = []
    for block in getattr(result, "content", ()):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            blocks.append(text)
            continue
        if hasattr(block, "model_dump"):
            blocks.append(
                json.dumps(block.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
            )
        else:
            blocks.append(str(block))
    if blocks:
        return "\n".join(blocks)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return ""
