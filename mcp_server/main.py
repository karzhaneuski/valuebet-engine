"""MCP server entry point for Hermes Agent.

Run via stdio:
    uv run python -m mcp_server.main
"""

from __future__ import annotations

from mcp_server.server import mcp

if __name__ == "__main__":
    mcp.run()
