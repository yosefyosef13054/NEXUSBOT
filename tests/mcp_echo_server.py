"""A tiny stdio MCP server used by the test-suite (`python tests/mcp_echo_server.py`)."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def shout(text: str) -> str:
    """Return the text in upper case with an exclamation mark."""
    return f"{text.upper()}!"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    server.run(transport="stdio")
