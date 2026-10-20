import asyncio
import os
from pathlib import Path
import signal
import statistics
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
import pytest_asyncio

from contracts.models import ToolCall
from harness.app import create_app
from harness.config import HarnessSettings
from harness.mcp_tools import McpServerConfig, McpToolRegistry
from harness.tools import ToolCatalog, ToolExecutor, ToolPolicy


def server(name="docs", **kwargs):
    return McpServerConfig(name=name, transport="stdio", command=sys.executable,
                           tool_groups=("test",), idempotent=True, **kwargs)


def result(text="ok", *, is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=is_error)


class Client:
    async def list_tools(self, *, cursor=None):
        return SimpleNamespace(tools=[SimpleNamespace(
            name="lookup", title=None, description="Lookup", input_schema={"type": "object"}
        )], next_cursor=None)

    async def call_tool(self, name, arguments):
        return result(arguments.get("query", "ok"))


@pytest_asyncio.fixture
async def registry_factory():
    registries = []

    def make(*args, **kwargs):
        registry = McpToolRegistry(*args, **kwargs)
        registries.append(registry)
        return registry

    yield make
    await asyncio.gather(*(registry.aclose() for registry in registries))


@pytest.mark.asyncio
async def test_discovery_and_calls_share_one_owner_task_and_context(registry_factory):
    opened, closed = [], []

    @asynccontextmanager
    async def factory(cfg):
        # An actual AnyIO context catches cross-task/cancel-scope ownership bugs.
        async with anyio.create_task_group():
            opened.append(asyncio.current_task())
            try:
                yield Client()
            finally:
                closed.append(asyncio.current_task())

    registry = registry_factory((server(),), client_factory=factory, schema_cache_ttl_seconds=0)
    selection = await registry.tools_for_groups({"test"})
    await registry.tools_for_groups({"test"})  # expired schemas reuse the same connection
    values = await asyncio.gather(*(selection.tools[0].handler({"query": str(i)}) for i in range(8)))
    assert [value.content for value in values] == [str(i) for i in range(8)]
    assert len(opened) == 1
    await asyncio.gather(registry.aclose(), registry.aclose())
    assert closed == opened
    assert not registry._clients
    with pytest.raises(RuntimeError, match="closed"):
        await registry.tools_for_groups({"test"})
    assert not (await selection.tools[0].handler({})).ok
    assert len(opened) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("idempotent, failures, expected_calls", [
    (True, 1, 2), (True, 2, 2), (False, 1, 1),
])
async def test_reconnect_replays_only_idempotent_calls_once(
    registry_factory, idempotent, failures, expected_calls
):
    opened, closed, calls = [], [], []

    class DisconnectingClient(Client):
        async def call_tool(self, name, arguments):
            calls.append(arguments)
            if len(calls) <= failures:
                raise BrokenPipeError("disconnected after possibly executing")
            return result()

    @asynccontextmanager
    async def factory(cfg):
        opened.append(cfg.name)
        try:
            yield DisconnectingClient()
        finally:
            closed.append(cfg.name)

    cfg = McpServerConfig(name="docs", transport="stdio", command="unused",
                          tool_groups=("test",), idempotent=idempotent)
    registry = registry_factory((cfg,), client_factory=factory)
    selection = await registry.tools_for_groups({"test"})
    lock = registry._locks["docs"]
    answer = await selection.tools[0].handler({})
    assert answer.ok == (idempotent and failures == 1)
    assert len(calls) == expected_calls
    assert registry._locks["docs"] is lock
    await registry.aclose()
    assert opened == closed


@pytest.mark.asyncio
async def test_tool_errors_are_not_replayed_or_treated_as_disconnects(registry_factory):
    from mcp.shared.exceptions import MCPError
    from mcp.types import INVALID_PARAMS

    calls, opened = [], []

    class ErrorClient(Client):
        async def call_tool(self, name, arguments):
            calls.append(arguments)
            if len(calls) == 1:
                raise MCPError(INVALID_PARAMS, "invalid tool arguments")
            if len(calls) == 2:
                return result("application failure", is_error=True)
            return result()

    @asynccontextmanager
    async def factory(cfg):
        opened.append(cfg.name)
        yield ErrorClient()

    registry = registry_factory((server(),), client_factory=factory)
    selection = await registry.tools_for_groups({"test"})
    handler = selection.tools[0].handler
    assert not (await handler({})).ok
    failed = await handler({})
    assert failed.error_code == "mcp_tool_error"
    assert (await handler({})).ok
    assert len(calls) == 3
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_failed_initialization_can_be_retried_on_next_discovery(registry_factory):
    attempts = 0

    @asynccontextmanager
    async def factory(cfg):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("configuration temporarily unavailable")
        yield Client()

    registry = registry_factory((server(),), client_factory=factory)
    assert (await registry.tools_for_groups({"test"})).diagnostics
    assert not registry._clients
    assert (await registry.tools_for_groups({"test"})).tools
    assert attempts == 2


@pytest.mark.asyncio
async def test_discovery_timeout_cleans_initializing_owner(registry_factory):
    entered, closed = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def factory(cfg):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield Client()
        finally:
            closed.set()

    registry = registry_factory((server(),), client_factory=factory, discovery_timeout_seconds=0.05)
    selection = await registry.tools_for_groups({"test"})
    assert selection.diagnostics[0].error_code == "mcp_discovery_timeout"
    assert entered.is_set() and closed.is_set()
    assert not registry._clients


@pytest.mark.asyncio
async def test_server_locks_serialize_across_runs_but_not_across_servers(registry_factory):
    active = {"docs": 0, "code": 0}
    peaks = dict(active)
    both_started = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient(Client):
        def __init__(self, name):
            self.name = name

        async def call_tool(self, name, arguments):
            active[self.name] += 1
            peaks[self.name] = max(peaks[self.name], active[self.name])
            try:
                if all(active.values()):
                    both_started.set()
                await release.wait()
                return result(arguments["query"])
            finally:
                active[self.name] -= 1

    @asynccontextmanager
    async def factory(cfg):
        yield BlockingClient(cfg.name)

    registry = registry_factory((server("docs"), server("code")), client_factory=factory)
    selection = await registry.tools_for_groups({"test"})
    # Direct handlers also verify registry-level serialization across ToolExecutors/runs.
    calls = [asyncio.create_task(selection.tools[i % 2].handler({"query": str(i)})) for i in range(8)]
    try:
        await asyncio.wait_for(both_started.wait(), 2)
    finally:
        release.set()
    answers = await asyncio.gather(*calls)
    assert [answer.content for answer in answers] == [str(i) for i in range(8)]
    assert peaks == {"docs": 1, "code": 1}


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_close_another_runs_active_connection(registry_factory):
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []

    class BlockingClient(Client):
        async def call_tool(self, name, arguments):
            entered.set()
            await release.wait()
            return result()

    @asynccontextmanager
    async def factory(cfg):
        try:
            yield BlockingClient()
        finally:
            closed.append(cfg.name)

    registry = registry_factory((server(),), client_factory=factory)
    handler = (await registry.tools_for_groups({"test"})).tools[0].handler
    active = asyncio.create_task(handler({}))
    await asyncio.wait_for(entered.wait(), 2)
    waiter = asyncio.create_task(handler({}))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not closed
    release.set()
    assert (await active).ok


@pytest.mark.asyncio
async def test_inflight_timeout_and_cancel_drop_connection_without_replay(registry_factory):
    entered = asyncio.Event()
    opened, closed, calls = [], [], []

    class BlockingClient(Client):
        async def call_tool(self, name, arguments):
            calls.append(arguments)
            if arguments.get("block"):
                entered.set()
                await asyncio.Event().wait()
            return result()

    @asynccontextmanager
    async def factory(cfg):
        opened.append(cfg.name)
        try:
            yield BlockingClient()
        finally:
            closed.append(cfg.name)

    registry = registry_factory((server(timeout_seconds=0.05),), client_factory=factory)
    tool = (await registry.tools_for_groups({"test"})).tools[0]
    catalog = ToolCatalog()
    catalog.register(tool.spec, tool.handler)
    executor = ToolExecutor(catalog, ToolPolicy())

    async def on_event(*args):
        pass

    values = await executor.execute_batch([
        ToolCall(name=tool.spec.name, arguments={"block": True})
    ], on_event)
    assert values[0].error_code == "tool_timeout"
    assert not registry._clients and len(calls) == 1
    assert opened == closed
    assert (await tool.handler({})).ok
    entered.clear()
    cancelled = asyncio.create_task(tool.handler({"block": True}))
    await asyncio.wait_for(entered.wait(), 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not registry._clients and opened == closed
    assert len(calls) == 3
    assert (await tool.handler({})).ok


@pytest.mark.asyncio
async def test_app_shutdown_cancels_runs_before_closing_registry(registry_factory, monkeypatch):
    entered, closed = asyncio.Event(), asyncio.Event()

    class BlockingClient(Client):
        async def call_tool(self, name, arguments):
            entered.set()
            await asyncio.Event().wait()

    @asynccontextmanager
    async def factory(cfg):
        try:
            yield BlockingClient()
        finally:
            closed.set()

    registry = registry_factory((server(),), client_factory=factory)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(McpToolRegistry, "from_file", lambda *args, **kwargs: registry)
    app = create_app(settings=HarnessSettings.from_environment())
    async with app.router.lifespan_context(app):
        handler = (await registry.tools_for_groups({"test"})).tools[0].handler
        run = asyncio.create_task(handler({}))
        app.state.tasks["test-run"] = run
        await asyncio.wait_for(entered.wait(), 2)
    assert run.cancelled() and closed.is_set()
    assert registry._closed and not registry._clients


@pytest.mark.asyncio
async def test_registry_close_settles_inflight_and_queued_requests(registry_factory):
    entered, closed = asyncio.Event(), asyncio.Event()
    opened = []

    class BlockingClient(Client):
        async def call_tool(self, name, arguments):
            entered.set()
            await asyncio.Event().wait()

    @asynccontextmanager
    async def factory(cfg):
        opened.append(cfg.name)
        try:
            yield BlockingClient()
        finally:
            closed.set()

    registry = registry_factory((server(),), client_factory=factory)
    handler = (await registry.tools_for_groups({"test"})).tools[0].handler
    active = asyncio.create_task(handler({}))
    await asyncio.wait_for(entered.wait(), 2)
    queued = asyncio.create_task(handler({}))
    await registry.aclose()
    values = await asyncio.wait_for(asyncio.gather(active, queued), 2)
    assert all(not value.ok for value in values)
    assert len(opened) == 1 and closed.is_set()
    assert not registry._clients


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_real_stdio_eight_calls_across_two_servers(registry_factory):
    fixture = Path(__file__).parent / "fixtures" / "mcp_connection_server.py"
    registry = registry_factory(tuple(server(name, args=(str(fixture),), timeout_seconds=10)
                                      for name in ("docs", "code")), discovery_timeout_seconds=10)
    selection = await registry.tools_for_groups({"test"})
    assert not selection.diagnostics
    handlers = {tool.spec.name: tool.handler for tool in selection.tools}
    pids = [int((await handlers[f"mcp__{name}__pid"]({})).content) for name in ("docs", "code")]
    assert pids[0] != pids[1]
    values = await asyncio.gather(*(handlers[f"mcp__{('docs', 'code')[i % 2]}__echo"](
        {"value": f"response-{i}", "delay": 0.01}) for i in range(8)))
    assert [value.content for value in values] == [f"response-{i}" for i in range(8)]
    await registry.aclose()
    if sys.platform != "win32":
        assert all(not process_alive(pid) for pid in pids)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal/reaping verification")
async def test_real_stdio_reuses_reconnects_cancels_and_reaps(registry_factory):
    fixture = Path(__file__).parent / "fixtures" / "mcp_connection_server.py"
    registry = registry_factory((server(args=(str(fixture),), timeout_seconds=10),),
                                discovery_timeout_seconds=10)
    cold_start = time.perf_counter()
    selection = await registry.tools_for_groups({"test"})
    cold_ms = (time.perf_counter() - cold_start) * 1000
    assert not selection.diagnostics
    handlers = {tool.spec.name.rsplit("__", 1)[1]: tool.handler for tool in selection.tools}
    pid = int((await handlers["pid"]({})).content)
    durations = []
    for i in range(10):
        start = time.perf_counter()
        assert (await handlers["echo"]({"value": str(i)})).content == str(i)
        durations.append((time.perf_counter() - start) * 1000)
    assert int((await handlers["pid"]({})).content) == pid
    print(f"cold_discovery_ms={cold_ms:.2f} hot_echo_p50_ms={statistics.median(durations):.2f} "
          f"hot_echo_max_ms={max(durations):.2f}")

    os.kill(pid, signal.SIGKILL)
    replacement = int((await handlers["pid"]({})).content)
    assert replacement != pid and not process_alive(pid)
    assert (await handlers["echo"]({"value": "recovered"})).content == "recovered"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(handlers["echo"]({"value": "cancel", "delay": 60}), 0.05)
    assert not process_alive(replacement)
    final_pid = int((await handlers["pid"]({})).content)
    assert final_pid != replacement
    await registry.aclose()
    assert not process_alive(final_pid)
