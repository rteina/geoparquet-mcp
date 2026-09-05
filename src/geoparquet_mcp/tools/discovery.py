"""Tools for finding out what is queryable, before querying it.

Handlers only: each one names an engine operation, hands it the arguments MCP
delivered plus the perimeter the application resolved, and returns what comes
back. There is no SQL in this file and no DuckDB connection — those belong to
`geoparquet_mcp.engine`.

Both tools here are cheap by construction. Describing a dataset reads Parquet
footers rather than data pages, and previewing rows stops after the first row
group. An agent can therefore orient itself in a 10 GB dataset without ever
fetching a data page — and, once the session has the footers cached, without
fetching anything at all.
"""

from __future__ import annotations

from typing import Any

from geoparquet_mcp import dependencies, engine

DESCRIBE_SOURCE = """\
Describe one remote dataset without reading any of its data: column names and \
types, the geometry and bbox columns, the coordinate reference system, the \
exact row count, the number of Parquet parts and row groups, the total remote \
size, and the geographic extent the dataset covers.

WHEN TO USE IT. Call this before your first filter against a dataset. It is \
how you learn the real column names — Overture nests many of them, so a \
category is `categories.primary` and a label is `names.primary`, not `category` \
and `name` — and how you check that the region you care about is inside \
`extent` before spending a query on it.

PARAMETERS.
  source: dataset name, from the `geoparquet://sources` resource or the \
default. Every other tool takes the same name.

WHAT COMES BACK. `columns` is a list of {name, type, role}, where role marks \
the geometry, bbox, name, category and confidence columns. `crs` is the \
coordinate reference system (OGC:CRS84 means plain longitude/latitude \
degrees, which is what every tool here expects) and `crs_is_default` says \
whether the file \
stated it or inherited the GeoParquet default. `extent` is the dataset's \
bounding box, computed from row-group statistics, or null when the file \
carries no statistics to compute it from. `row_count`, `remote_files`, \
`row_groups` and `remote_bytes` describe the physical file. `scan` reports the \
bytes this call itself pulled: Parquet footers only, never a data page. On \
Overture places that is about 26 MB the first time — the footers of 16 parts \
carrying 4096 row groups of statistics — and zero afterwards, because the \
session caches them. Either way it is metadata about a 10.5 GB file, not the \
file."""

PREVIEW_ROWS = """\
Return the first few rows of a dataset, so you can see what the values \
actually look like.

WHEN TO USE IT. After `geoparquet_describe_source` tells you a column exists \
and before you filter on it, to learn how it is populated: what a category \
string looks like in practice, whether a field is mostly null, how an address \
is spelled. Guessing a filter value and getting zero features back costs more \
than one preview.

This is NOT a spatial question. The rows are whatever the file stores first, \
in no geographic order and in no ranking — do not read them as "the most \
important places" or "places near anywhere". To ask where things are, use \
`geoparquet_filter_spatial` or `geoparquet_find_nearest`.

PARAMETERS.
  source: dataset name.
  columns: column expressions to return, for example ["id", "names.primary", \
"confidence"]. Omit for the dataset's default projection.
  limit: how many rows, 1 to 100. Ten is usually enough to see the shape.

WHAT COMES BACK. `rows` as plain records, `columns_returned` naming the keys, \
and the `scan` block. The read stops at the first row group of the first part \
file, so the cost does not grow with the dataset."""


def describe_source(source: str = engine.DEFAULT_SOURCE) -> dict[str, Any]:
    """Return one dataset's schema, CRS, extent and physical footprint."""
    return engine.dataset_schema(source=source, **dependencies.engine_kwargs())


def preview_rows(
    source: str = engine.DEFAULT_SOURCE,
    columns: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Return the first few rows of a dataset, to see what the values look like."""
    return engine.preview_rows(
        source=source, columns=columns, limit=limit, **dependencies.engine_kwargs()
    )


def register(server) -> None:
    server.tool(name="geoparquet_describe_source", description=DESCRIBE_SOURCE)(describe_source)
    server.tool(name="geoparquet_preview_rows", description=PREVIEW_ROWS)(preview_rows)
