"""Spatial analysis executed inside the remote Parquet file.

Every tool here turns a spatial question into a predicate over the dataset's
`bbox` struct. That matters more than it looks: Parquet stores per-row-group
min/max statistics for `bbox.xmin`, `bbox.xmax`, `bbox.ymin` and `bbox.ymax`,
so DuckDB can decide from the footer alone which row groups can possibly
contain the answer, and never request the bytes of the others. Combined with
column projection, a city-scale question against a continental dataset costs
a few megabytes of HTTP range requests.

The rule the whole module follows: whatever the user-facing question is,
derive a rectangle from it first, because a rectangle is the only thing
Parquet statistics can prune on.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from geoparquet_mcp import sources
from geoparquet_mcp.duckdb_session import (
    clamp_limit,
    connect,
    fetch_records,
    measured,
)

EARTH_RADIUS_KM = 6371.0088


class BoundingBox(BaseModel):
    """A geographic rectangle in WGS 84 degrees (EPSG:4326 / OGC:CRS84)."""

    min_lon: float = Field(ge=-180, le=180, description="Western edge, degrees")
    min_lat: float = Field(ge=-90, le=90, description="Southern edge, degrees")
    max_lon: float = Field(ge=-180, le=180, description="Eastern edge, degrees")
    max_lat: float = Field(ge=-90, le=90, description="Northern edge, degrees")

    @model_validator(mode="after")
    def _ordered(self) -> BoundingBox:
        if self.min_lon >= self.max_lon:
            raise ValueError("min_lon must be smaller than max_lon")
        if self.min_lat >= self.max_lat:
            raise ValueError("min_lat must be smaller than max_lat")
        return self

    def predicate(self, column: str = "bbox") -> str:
        """SQL for "this feature's bounding box intersects mine".

        Written as four independent comparisons on the four bbox members so
        that each one can prune row groups on its own statistics. An
        equivalent expression using a geometry function would be correct but
        opaque to the Parquet reader, and would read the whole file.
        """
        return (
            f"{column}.xmin <= {self.max_lon} AND {column}.xmax >= {self.min_lon} "
            f"AND {column}.ymin <= {self.max_lat} AND {column}.ymax >= {self.min_lat}"
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "min_lon": self.min_lon,
            "min_lat": self.min_lat,
            "max_lon": self.max_lon,
            "max_lat": self.max_lat,
        }


def _bbox_around(lon: float, lat: float, radius_km: float) -> BoundingBox:
    """The smallest lon/lat rectangle containing a circle on the sphere.

    Used to give a radius query something Parquet can prune on before the
    exact distance test runs over the surviving rows.
    """
    lat_delta = math.degrees(radius_km / EARTH_RADIUS_KM)
    # Longitude degrees shrink towards the poles; guard against the cos going
    # to zero near them by falling back to the whole longitude range.
    cos_lat = math.cos(math.radians(lat))
    lon_delta = 180.0 if abs(cos_lat) < 1e-9 else min(180.0, lat_delta / abs(cos_lat))
    return BoundingBox(
        min_lon=max(-180.0, lon - lon_delta),
        min_lat=max(-90.0, lat - lat_delta),
        max_lon=min(180.0, lon + lon_delta),
        max_lat=min(90.0, lat + lat_delta),
    )


def _projection(definition: sources.Source, columns: list[str] | None) -> str:
    """Column list for the scan.

    Never `SELECT *`: the projection is pushed into the Parquet reader, and on
    a wide dataset such as Overture places it is worth as much as the spatial
    filter.
    """
    if columns:
        return ", ".join(columns)
    if definition.default_columns:
        return ", ".join(definition.default_columns)
    return "*"


def _attribute_filters(
    definition: sources.Source,
    category: str | None,
    name_contains: str | None,
    min_confidence: float | None,
) -> tuple[list[str], list[Any]]:
    """Optional non-spatial predicates, as SQL fragments plus bound values."""
    clauses: list[str] = []
    params: list[Any] = []
    has_categories = definition.theme == "places"
    if category:
        if not has_categories:
            raise ValueError(f"source {definition.name!r} has no category column")
        clauses.append("categories.primary = ?")
        params.append(category)
    if name_contains:
        clauses.append("lower(names.primary) LIKE lower(?)")
        params.append(f"%{name_contains}%")
    if min_confidence is not None:
        if not has_categories:
            raise ValueError(f"source {definition.name!r} has no confidence column")
        clauses.append("confidence >= ?")
        params.append(min_confidence)
    return clauses, params


def bbox_query(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    source: str = sources.DEFAULT_SOURCE,
    category: str | None = None,
    name_contains: str | None = None,
    min_confidence: float | None = None,
    columns: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return features whose bounding box intersects a lon/lat rectangle.

    The rectangle is pushed into the remote Parquet file as row-group pruning;
    only the matching byte ranges are fetched.
    """
    definition = sources.get_source(source)
    box = BoundingBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)
    target = definition.scan_target(sources.resolve_release())
    row_limit = clamp_limit(limit)

    clauses, params = _attribute_filters(definition, category, name_contains, min_confidence)
    where = " AND ".join([box.predicate(definition.bbox_column), *clauses])
    sql = (
        f"SELECT {_projection(definition, columns)} "
        f"FROM read_parquet('{target}') "
        f"WHERE {where} "
        f"LIMIT {row_limit}"
    )

    con = connect()
    try:
        with measured(con) as report:
            rows = fetch_records(con, sql, params)
    finally:
        con.close()

    return {
        "source": definition.name,
        "bbox": box.as_dict(),
        "row_count": len(rows),
        "limit": row_limit,
        "truncated": len(rows) == row_limit,
        "rows": rows,
        "sql": sql,
        "scan": report.as_dict(),
        "attribution": definition.attribution,
    }


def bbox_aggregate(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    source: str = sources.DEFAULT_SOURCE,
    group_by: str | None = "categories.primary",
    limit: int = 25,
) -> dict[str, Any]:
    """Count features inside a rectangle, optionally grouped by a column.

    An aggregate is where remote reading pays off most: the answer is a
    handful of rows, and the scan still never leaves the matching row groups.
    """
    definition = sources.get_source(source)
    box = BoundingBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)
    target = definition.scan_target(sources.resolve_release())
    row_limit = clamp_limit(limit)
    where = box.predicate(definition.bbox_column)

    if group_by:
        sql = (
            f"SELECT {group_by} AS group_value, count(*) AS feature_count "
            f"FROM read_parquet('{target}') WHERE {where} "
            f"GROUP BY 1 ORDER BY feature_count DESC LIMIT {row_limit}"
        )
    else:
        sql = f"SELECT count(*) AS feature_count FROM read_parquet('{target}') WHERE {where}"

    con = connect()
    try:
        with measured(con) as report:
            rows = fetch_records(con, sql)
    finally:
        con.close()

    return {
        "source": definition.name,
        "bbox": box.as_dict(),
        "group_by": group_by,
        "groups": rows,
        "sql": sql,
        "scan": report.as_dict(),
        "attribution": definition.attribution,
    }


def nearest(
    lon: float,
    lat: float,
    radius_km: float = 1.0,
    source: str = sources.DEFAULT_SOURCE,
    category: str | None = None,
    name_contains: str | None = None,
    columns: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return the closest features to a point, nearest first.

    A radius is not something Parquet statistics understand, so the search
    circle is first widened to its bounding rectangle, which *is* prunable.
    The exact great-circle distance is then computed only over the rows that
    survive, and used both to filter and to order.
    """
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    definition = sources.get_source(source)
    box = _bbox_around(lon, lat, radius_km)
    target = definition.scan_target(sources.resolve_release())
    row_limit = clamp_limit(limit)

    clauses, params = _attribute_filters(definition, category, name_contains, None)
    distance = f"ST_Distance_Sphere({definition.geometry_column}, ST_Point({lon}, {lat})) / 1000.0"
    where = " AND ".join([box.predicate(definition.bbox_column), *clauses])
    # The distance test lives in an outer query so that the inner scan carries
    # only the prunable bbox predicate; DuckDB still pushes the inner WHERE
    # into the Parquet reader.
    sql = (
        f"SELECT * FROM ("
        f"SELECT {_projection(definition, columns)}, "
        f"round({distance}, 4) AS distance_km "
        f"FROM read_parquet('{target}') "
        f"WHERE {where}"
        f") WHERE distance_km <= {radius_km} "
        f"ORDER BY distance_km "
        f"LIMIT {row_limit}"
    )

    con = connect()
    try:
        with measured(con) as report:
            rows = fetch_records(con, sql, params)
    finally:
        con.close()

    return {
        "source": definition.name,
        "center": {"lon": lon, "lat": lat},
        "radius_km": radius_km,
        "search_bbox": box.as_dict(),
        "row_count": len(rows),
        "rows": rows,
        "sql": sql,
        "scan": report.as_dict(),
        "attribution": definition.attribution,
    }


def pushdown_report(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    source: str = sources.DEFAULT_SOURCE,
    mode: Literal["single_file", "whole_dataset"] = "single_file",
) -> dict[str, Any]:
    """Measure what predicate pushdown saves, by running the query both ways.

    The same aggregate runs twice against the same remote data: once normally,
    once with DuckDB's filter pushdown optimizer disabled so that every row
    group is fetched. The difference is the claim this project makes, produced
    on demand rather than quoted from a README.

    `single_file` restricts both runs to one Parquet part so the unpushed run
    stays affordable; `whole_dataset` measures the pushed run against every
    part and compares it to the dataset's total remote size instead.
    """
    definition = sources.get_source(source)
    box = BoundingBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)
    release = sources.resolve_release()
    target = definition.scan_target(release)
    where = box.predicate(definition.bbox_column)

    con = connect()
    try:
        # Footer metadata: total remote size, and which single part holds the
        # most matches when a single-file comparison is requested.
        with measured(con) as metadata_report:
            footprint = fetch_records(
                con,
                f"""
                SELECT count(*) AS remote_files, sum(file_size_bytes) AS remote_bytes
                FROM parquet_file_metadata('{target}')
                """,
            )[0]
            if mode == "single_file":
                busiest = fetch_records(
                    con,
                    f"""
                    SELECT filename, count(*) AS matches
                    FROM read_parquet('{target}', filename = true)
                    WHERE {where}
                    GROUP BY 1 ORDER BY matches DESC LIMIT 1
                    """,
                )
            else:
                busiest = []

        if mode == "single_file":
            if not busiest:
                raise ValueError("no features match this bounding box; widen it and retry")
            scan_target = busiest[0]["filename"]
            file_bytes = fetch_records(
                con,
                f"SELECT file_size_bytes FROM parquet_file_metadata('{scan_target}')",
            )[0]["file_size_bytes"]
        else:
            scan_target = target
            file_bytes = footprint["remote_bytes"]

        # The measured query. `max(length(...))` forces a real column read, so
        # the comparison is not answered from Parquet metadata alone.
        measured_sql = (
            f"SELECT count(*) AS matches, max(length(names.primary)) AS longest_name "
            f"FROM read_parquet('{scan_target}') WHERE {where}"
        )

        with measured(con) as pushed_report:
            pushed_rows = fetch_records(con, measured_sql)

        unpushed: dict[str, Any] | None = None
        if mode == "single_file":
            con.execute("SET disabled_optimizers='filter_pushdown'")
            try:
                with measured(con) as unpushed_report:
                    fetch_records(con, measured_sql)
            finally:
                con.execute("SET disabled_optimizers=''")
            unpushed = unpushed_report.as_dict()
    finally:
        con.close()

    pushed_bytes = pushed_report.bytes_scanned
    result: dict[str, Any] = {
        "source": definition.name,
        "release": release,
        "mode": mode,
        "bbox": box.as_dict(),
        "scan_target": scan_target,
        "matches": pushed_rows[0]["matches"],
        "sql": measured_sql,
        "remote_files": footprint["remote_files"],
        "dataset_remote_bytes": footprint["remote_bytes"],
        "baseline_bytes_if_downloaded": file_bytes,
        "with_pushdown": pushed_report.as_dict(),
        "without_pushdown": unpushed,
        "metadata_scan": metadata_report.as_dict(),
    }
    if pushed_bytes:
        result["download_avoided_ratio"] = round(file_bytes / pushed_bytes, 1)
        if unpushed:
            result["pushdown_ratio"] = round(unpushed["bytes_scanned"] / pushed_bytes, 1)
    return result


def register(server) -> None:
    server.tool(
        name="bbox_query",
        description=(
            "Return features from a remote GeoParquet dataset whose bounding box "
            "intersects a lon/lat rectangle, with optional category, name and "
            "confidence filters. The rectangle is pushed into the Parquet file, so "
            "only matching row groups are fetched. Every result reports the bytes "
            "actually read."
        ),
    )(bbox_query)
    server.tool(
        name="bbox_aggregate",
        description=(
            "Count features inside a lon/lat rectangle, optionally grouped by a column "
            "such as categories.primary. Answers 'what kind of places are here' without "
            "transferring the features themselves."
        ),
    )(bbox_aggregate)
    server.tool(
        name="nearest",
        description=(
            "Return the features closest to a point within a radius in kilometres, "
            "nearest first, with the great-circle distance of each."
        ),
    )(nearest)
    server.tool(
        name="pushdown_report",
        description=(
            "Prove the pushdown: run the same spatial aggregate against the remote file "
            "with and without DuckDB's filter pushdown and report the bytes each one "
            "pulled over HTTP, alongside the size of the file a download-first workflow "
            "would have moved."
        ),
    )(pushdown_report)
