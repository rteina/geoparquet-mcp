"""Tools that let an agent find out what is queryable, before querying it.

Both tools here are deliberately cheap: listing sources touches the network
only to resolve the current release, and describing one reads Parquet footers
rather than data pages. An agent can therefore orient itself in a 10 GB
dataset for the cost of a few hundred kilobytes.
"""

from __future__ import annotations

from typing import Any

from geoparquet_mcp import sources
from geoparquet_mcp.duckdb_session import connect, fetch_records, measured


def list_sources() -> dict[str, Any]:
    """Return the catalogue of remote GeoParquet datasets this server can read."""
    return {
        "release": sources.resolve_release(),
        "default_source": sources.DEFAULT_SOURCE,
        "sources": sources.describe_sources(),
    }


def describe_source(source: str = sources.DEFAULT_SOURCE) -> dict[str, Any]:
    """Return columns, exact row count and remote size for one source.

    Row counts and file sizes come from Parquet footer metadata, so this reads
    kilobytes of a multi-gigabyte dataset. The `scan` block reports what it
    actually cost.
    """
    definition = sources.get_source(source)
    release = sources.resolve_release()
    target = definition.scan_target(release)

    con = connect()
    try:
        with measured(con) as report:
            schema = fetch_records(con, f"DESCRIBE SELECT * FROM read_parquet('{target}')")
            footprint = fetch_records(
                con,
                f"""
                SELECT
                    count(*)                AS remote_files,
                    sum(num_rows)           AS row_count,
                    sum(file_size_bytes)    AS remote_bytes,
                    sum(num_row_groups)     AS row_groups
                FROM parquet_file_metadata('{target}')
                """,
            )[0]
    finally:
        con.close()

    return {
        "source": definition.name,
        "title": definition.title,
        "description": definition.description,
        "license": definition.license,
        "attribution": definition.attribution,
        "release": release,
        "scan_target": target,
        "https_prefix": definition.https_prefix(release),
        "columns": [{"name": row["column_name"], "type": row["column_type"]} for row in schema],
        "bbox_column": definition.bbox_column,
        "geometry_column": definition.geometry_column,
        "default_columns": list(definition.default_columns),
        "remote_files": footprint["remote_files"],
        "row_count": footprint["row_count"],
        "remote_bytes": footprint["remote_bytes"],
        "row_groups": footprint["row_groups"],
        "notes": definition.notes,
        "scan": report.as_dict(),
    }


def register(server) -> None:
    server.tool(
        name="list_sources",
        description=(
            "List the remote GeoParquet datasets this server can query, with licence, "
            "current release path and approximate size. Call this first."
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
