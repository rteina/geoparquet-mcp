"""Tool families exposed over MCP.

Handlers, not behaviour: every tool in here validates nothing, computes
nothing and holds no SQL. It names an operation in `geoparquet_mcp.engine`,
passes on what the client sent, and returns the result. The engine is where
the work and the input validation live, and it can be used without any of
this.

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
