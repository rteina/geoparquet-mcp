"""Tool families exposed over MCP.

One module per family: `discovery` answers "what can I query?", `spatial`
answers "what is here?". Each module exposes `register(server)` so
`server.py` stays a wiring file.
"""

from __future__ import annotations

from geoparquet_mcp.tools import discovery, spatial

__all__ = ["discovery", "spatial", "register_all"]


def register_all(server) -> None:
    """Attach every tool family to an MCP server instance."""
    discovery.register(server)
    spatial.register(server)
