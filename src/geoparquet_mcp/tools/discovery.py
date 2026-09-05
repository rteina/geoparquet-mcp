"""Tools that let an agent find out what is queryable, before querying it.

Handlers only: each one names an engine operation, hands it the arguments MCP
delivered, and returns what comes back. There is no SQL in this file and no
DuckDB connection — those belong to `geoparquet_mcp.engine`.

All three tools here are deliberately cheap: listing datasets touches the
network only to resolve the current release, and describing one reads Parquet
footers rather than data pages. An agent can therefore orient itself in a
10 GB dataset for the cost of a few hundred kilobytes.
"""

from __future__ import annotations

from typing import Any

from geoparquet_mcp import engine


def list_sources(exact: bool = False) -> dict[str, Any]:
    """List the remote GeoParquet datasets this server can query."""
    return engine.list_datasets(exact=exact)


def describe_source(source: str = engine.DEFAULT_SOURCE) -> dict[str, Any]:
    """Return the columns, types, row count and remote size of one dataset."""
    return engine.dataset_schema(source=source)


def dataset_extent(source: str = engine.DEFAULT_SOURCE) -> dict[str, Any]:
    """Return the geographic bounding box covered by one dataset."""
    return engine.dataset_extent(source=source)


def register(server) -> None:
    server.tool(
        name="list_sources",
        description=(
            "List the remote GeoParquet datasets this server can query, with licence, "
            "current release path and size. Call this first. Pass exact=true to read "
            "the true row counts from Parquet footers instead of the registry's "
            "approximations; that costs one request per part file."
        ),
    )(list_sources)
    server.tool(
        name="describe_source",
        description=(
            "Return the column schema, exact row count and remote byte size of a source, "
            "read from Parquet footer metadata only. Use it to discover column names "
            "before filtering."
        ),
    )(describe_source)
    server.tool(
        name="dataset_extent",
        description=(
            "Return the bounding box a dataset covers, computed from Parquet row-group "
            "statistics without reading any geometry. Use it to check whether a region "
            "is covered before querying it."
        ),
    )(dataset_extent)
