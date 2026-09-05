"""Spatial analysis tools, as MCP handlers over the engine.

Handlers only. Every function here takes the arguments an MCP client sent,
calls one engine operation, and returns its result. The spatial reasoning —
how a rectangle becomes a prunable Parquet predicate, how a radius becomes a
rectangle, how features become H3 cells — lives in
`geoparquet_mcp.engine.operations`, which knows nothing about MCP and can be
used and tested without it.

The engine validates its own inputs and raises typed errors with actionable
messages, so these handlers deliberately add no validation of their own:
duplicating it here is how the two copies drift apart.
"""

from __future__ import annotations

from typing import Any

from geoparquet_mcp import engine

# Re-exported so callers that imported the bounding box from the tool layer
# keep working; the definition itself belongs to the engine.
BoundingBox = engine.BoundingBox


def bbox_query(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    source: str = engine.DEFAULT_SOURCE,
    category: str | None = None,
    name_contains: str | None = None,
    min_confidence: float | None = None,
    columns: list[str] | None = None,
    include_geometry: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Return features intersecting a lon/lat rectangle, as GeoJSON."""
    return engine.bbox_query(
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        source=source,
        category=category,
        name_contains=name_contains,
        min_confidence=min_confidence,
        columns=columns,
        include_geometry=include_geometry,
        limit=limit,
    )


def nearest(
    lon: float,
    lat: float,
    radius_km: float = 1.0,
    source: str = engine.DEFAULT_SOURCE,
    category: str | None = None,
    name_contains: str | None = None,
    columns: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return the features closest to a point within a radius, nearest first."""
    return engine.nearest(
        lon=lon,
        lat=lat,
        radius_km=radius_km,
        source=source,
        category=category,
        name_contains=name_contains,
        columns=columns,
        limit=limit,
    )


def column_statistics(
    column: str,
    source: str = engine.DEFAULT_SOURCE,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    top_k: int = 25,
    histogram_buckets: int = 10,
) -> dict[str, Any]:
    """Summarise one column over a rectangle: count, range and distribution."""
    return engine.column_statistics(
        column=column,
        source=source,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        top_k=top_k,
        histogram_buckets=histogram_buckets,
    )


def h3_aggregate(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    resolution: int = 8,
    source: str = engine.DEFAULT_SOURCE,
    limit: int = 200,
    include_cell_centre: bool = True,
) -> dict[str, Any]:
    """Bin features into H3 cells and return the count per cell."""
    return engine.h3_aggregate(
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        resolution=resolution,
        source=source,
        limit=limit,
        include_cell_centre=include_cell_centre,
    )


def point_in_polygon(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    point_source: str = engine.DEFAULT_SOURCE,
    polygon_source: str = "overture_divisions",
    polygon_subtype: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Count the features of one dataset falling inside each polygon of another."""
    return engine.point_in_polygon(
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        point_source=point_source,
        polygon_source=polygon_source,
        polygon_subtype=polygon_subtype,
        limit=limit,
    )


def register(server) -> None:
    server.tool(
        name="bbox_query",
        description=(
            "Return features from a remote GeoParquet dataset whose bounding box "
            "intersects a lon/lat rectangle, as a GeoJSON FeatureCollection, with "
            "optional category, name and confidence filters. The rectangle is pushed "
            "into the Parquet file, so only matching row groups are fetched. Every "
            "result reports the bytes actually read. Pass include_geometry=false to "
            "skip the geometry column and approximate each feature by its bbox corner."
        ),
    )(bbox_query)
    server.tool(
        name="nearest",
        description=(
            "Return the features closest to a point within a radius in kilometres, "
            "nearest first, with the great-circle distance of each."
        ),
    )(nearest)
    server.tool(
        name="column_statistics",
        description=(
            "Summarise one column of a dataset, optionally restricted to a lon/lat "
            "rectangle. A numeric column returns count, min, max, mean, standard "
            "deviation, quartiles and a histogram; a categorical column returns the "
            "most frequent values with their share. Answers 'what kind of places are "
            "here' without transferring the features themselves. Always pass a "
            "bounding box unless you really want the whole dataset scanned."
        ),
    )(column_statistics)
    server.tool(
        name="h3_aggregate",
        description=(
            "Bin the features inside a lon/lat rectangle into H3 hexagonal cells at a "
            "given resolution (0 continent-sized to 15 sub-metre; 8 is a neighbourhood, "
            "9 a block) and return the feature count per cell with the cell centre. Use "
            "it to find density hotspots without downloading the features."
        ),
    )(h3_aggregate)
    server.tool(
        name="point_in_polygon",
        description=(
            "Count how many features of a point dataset fall inside each polygon of a "
            "polygonal dataset, within a lon/lat rectangle. Both datasets are pruned by "
            "the rectangle before the containment test runs. Use polygon_subtype to pick "
            "one administrative level, for example 'locality' or 'county'."
        ),
    )(point_in_polygon)
