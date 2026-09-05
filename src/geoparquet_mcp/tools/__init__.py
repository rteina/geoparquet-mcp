"""Tool families exposed over MCP.

Handlers, not behaviour: every tool in here validates nothing, computes
nothing and holds no SQL — not in its code and not in its prose. It names an
operation in `geoparquet_mcp.engine`, adds the perimeter the application
resolved and injected, and returns the result. The engine is where the work
and the input validation live, and it can be used without any of this.

One module per family: `discovery` answers "what can I query?", `spatial`
answers "what is there?", and `sql` is the escape hatch for the question
nobody anticipated. Each module exposes `register(server)` so `server.py`
stays a wiring file.
"""

from __future__ import annotations

from geoparquet_mcp.tools import discovery, spatial, sql

__all__ = ["discovery", "register_all", "spatial", "sql"]


def register_all(server) -> None:
    """Attach every tool family to an MCP server instance."""
    discovery.register(server)
    spatial.register(server)
    sql.register(server)
