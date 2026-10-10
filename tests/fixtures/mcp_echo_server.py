from mcp.server import MCPServer


mcp = MCPServer("Agent-X integration test")


@mcp.tool()
def echo(value: str) -> str:
    """Echo a value through a real MCP stdio connection."""
    return f"mcp:{value}"


if __name__ == "__main__":
    mcp.run()
