"""Real subprocess fixture for persistent-connection lifecycle tests."""
import asyncio
import os

from mcp.server import MCPServer


mcp = MCPServer("Agent-X connection lifecycle test")


@mcp.tool()
async def echo(value: str, delay: float = 0) -> str:
    """Echo after an optional delay, allowing in-flight cancellation tests."""
    await asyncio.sleep(delay)
    return value


@mcp.tool()
def pid() -> int:
    """Identify the subprocess to verify reuse, crash recovery and reaping."""
    return os.getpid()


if __name__ == "__main__":
    mcp.run()
