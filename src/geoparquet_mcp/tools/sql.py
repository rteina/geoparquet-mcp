"""The escape hatch: read-only SQL over the datasets in scope.

A handler, like the others. The parsing, the guard that keeps an ad-hoc query
inside the perimeter, and the row and byte ceilings all live in
`geoparquet_mcp.engine.query` — this file names that operation and formats
nothing.
"""

from __future__ import annotations

from typing import Any

from geoparquet_mcp import dependencies, engine
from geoparquet_mcp.engine.query import USAGE


def run_sql(
    sql: str,
    max_rows: int = 100,
    max_bytes: int = engine.DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Run one read-only query against the datasets in scope."""
    return engine.run_sql(
        sql=sql, max_rows=max_rows, max_bytes=max_bytes, **dependencies.engine_kwargs()
    )


def register(server) -> None:
    server.tool(name="geoparquet_run_sql", description=USAGE)(run_sql)
