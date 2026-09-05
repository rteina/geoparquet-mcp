"""Spatial analysis tools, as MCP handlers over the engine.

Handlers only. Every function here takes the arguments an MCP client sent,
adds the perimeter the application resolved, calls one engine operation, and
returns its result. The spatial reasoning — how a rectangle becomes a prunable
Parquet predicate, how a WKT polygon is reduced to an envelope and then
re-tested exactly, how a radius becomes a rectangle, how features become H3
cells — lives in `geoparquet_mcp.engine.operations`, which knows nothing about
MCP and can be used and tested without it.

The engine validates its own inputs and raises typed errors with actionable
messages, so these handlers deliberately add no validation of their own:
duplicating it here is how the two copies drift apart.
"""

from __future__ import annotations

from typing import Any

from geoparquet_mcp import dependencies, engine

# Re-exported so callers that imported the bounding box from the tool layer
# keep working; the definition itself belongs to the engine.
BoundingBox = engine.BoundingBox

FILTER_SPATIAL = """\
Return the features of a dataset that fall inside an area, as a GeoJSON \
FeatureCollection. The area is either a lon/lat rectangle or an arbitrary WKT \
geometry.

WHEN TO USE IT. When the answer is the features themselves — "which cafes are \
in this neighbourhood", "give me the buildings along this street" — and you \
intend to look at them individually. When you only need a count, a ranking or \
a distribution, use `geoparquet_aggregate_attribute` or \
`geoparquet_summarize_h3` instead: they answer from the remote file and \
transfer kilobytes instead of features.

COST. The rectangle is what makes the read cheap. It is pushed into the remote \
Parquet file and prunes whole row groups from their footer statistics before \
any byte of data is fetched, so a tight box costs far less than a wide one — \
this is the difference between megabytes and gigabytes, not a micro-optimisation. \
Always pass the tightest area the question allows.

PARAMETERS.
  source: dataset name.
  min_lon, min_lat, max_lon, max_lat: the rectangle, in WGS 84 degrees. Pass \
all four, or none if you are using `wkt`.
  wkt: an arbitrary geometry instead of a rectangle, for example \
'POLYGON ((2.33 48.85, 2.36 48.85, 2.36 48.87, 2.33 48.87, 2.33 48.85))'. Its \
envelope prunes the read and the exact shape then filters the survivors, so \
the answer is exact. Give either a rectangle or a `wkt`, never both.
  category: exact match on the dataset's category column, for example \
'restaurant'. Preview the column first — the vocabulary is not obvious.
  name_contains: case-insensitive substring of the feature name.
  min_confidence: 0 to 1, Overture's own confidence in the record. 0.8 drops \
most questionable entries.
  columns: column expressions to return. A narrow projection is worth as much \
as a tight box, because Parquet is columnar and unread columns are unfetched.
  include_geometry: false skips the geometry column — the widest in the file — \
and approximates each feature by its bounding-box corner, which is exact for \
points. Ignored when `wkt` is used, since the exact test needs the geometry.
  limit: maximum features, capped at 1000.

WHAT COMES BACK. `geojson` as a FeatureCollection; `feature_count` and \
`truncated`, which tells you the limit was reached and there is more; \
`geometry_is_exact`; the `sql` that ran; and `scan` with `bytes_scanned` — read \
it, and tighten the area if it looks large."""

FIND_NEAREST = """\
Return the features closest to a point, nearest first, each with its \
great-circle distance in kilometres.

WHEN TO USE IT. For "what is near here" and "which is the closest" — the \
questions where the ranking and the distance are the answer. Use \
`geoparquet_filter_spatial` instead when you want everything in an area \
rather than the closest few.

HOW IT STAYS CHEAP. A radius is not something Parquet statistics can prune on, \
so the search circle is first widened to its bounding rectangle, which is \
prunable; the exact distance is then computed only over the rows that survive, \
and used both to filter and to order. A large radius therefore costs a large \
read: prefer the smallest radius that can contain the answer, and widen it \
only if you come back empty.

PARAMETERS.
  source: dataset name.
  lon, lat: the centre point, in WGS 84 degrees. Longitude first.
  radius_km: how far to look, up to 500. Results outside it are excluded, so \
this is a filter, not just a hint.
  category, name_contains: the same narrowing as `geoparquet_filter_spatial`.
  columns: column expressions to return.
  limit: how many neighbours, capped at 1000.

WHAT COMES BACK. `rows`, ordered nearest first, each carrying `distance_km`; \
the `search_bbox` actually used for pruning; and the `scan` block."""

AGGREGATE_ATTRIBUTE = """\
Group the rows of a dataset by one column and aggregate them, optionally \
inside a lon/lat rectangle. The grouping runs inside the remote file.

WHEN TO USE IT. For "how many of each", "what is the average", "which is the \
most common" — any question whose answer is a table of groups rather than a \
set of features. This is the tool that makes a 10 GB dataset answerable in \
kilobytes: the grouping happens remotely and only the group rows cross the \
network, however many rows went into them. Reach for it before \
`geoparquet_filter_spatial` whenever counting would do.

PARAMETERS.
  source: dataset name.
  group_by: the column whose distinct values become the groups, for example \
'categories.primary'.
  aggregate: one of count, sum, avg, min, max. Default count.
  measure: the column to aggregate. Required for sum, avg, min and max, and \
rejected for count, which counts rows. It must be numeric; a non-numeric one \
is refused before any byte is fetched.
  min_lon, min_lat, max_lon, max_lat: restrict the aggregate to a rectangle. \
Pass all four or none. Omitting them aggregates the entire dataset, which \
reads the grouped and measured columns in full — slow and expensive on a \
multi-gigabyte source. Pass a box unless you truly mean the whole world.
  limit: maximum groups returned, ordered by the aggregate descending.

WHAT COMES BACK. `groups`, each with `group_value`, `value` (the aggregate) \
and `row_count` (rows in the group); `rows_aggregated`; `truncated`; the `sql` \
that ran; and the `scan` block."""

SUMMARIZE_H3 = """\
Bin the features inside a rectangle into H3 hexagonal cells and return the \
count per cell. A density map, computed remotely.

WHEN TO USE IT. For "where are these densest", "how is this spread across the \
city", and anything you would answer with a heatmap. The binning and counting \
happen inside the remote file, so the answer is a few hundred cells whether \
they cover a thousand features or ten million — you never transfer the \
features to find out where they cluster.

PARAMETERS.
  source: dataset name.
  min_lon, min_lat, max_lon, max_lat: the rectangle to bin, in WGS 84 degrees. \
All four are required; this tool has no whole-world mode by design.
  resolution: the H3 level, 0 to 15. 0 is continent-sized, 6 is a city, 8 is \
roughly a neighbourhood, 9 a block, 11 a building. Choosing too fine a \
resolution for a wide box returns thousands of near-empty cells; start at 8 \
for a city and adjust.
  limit: maximum cells, ordered by count descending, so the limit keeps the \
hotspots.
  include_cell_centre: adds the latitude and longitude of each cell's centre, \
which is what you need to plot the result.

WHAT COMES BACK. `cells`, each with `h3_cell` (the canonical hexadecimal id), \
`feature_count` and optionally the centre; plus `features_binned`, \
`truncated`, and the `scan` block.

Features are binned on their bounding-box centre rather than their true \
geometry, which avoids fetching the widest column in the file: exact for point \
datasets, the envelope's centre for polygonal ones. Requires DuckDB's H3 \
extension; if it cannot be loaded the tool says so rather than falling back to \
something slower."""


COUNT_IN_POLYGONS = """\
Count how many features of one dataset fall inside each polygon of another: a \
point-in-polygon join between two remote datasets, restricted to a rectangle.

WHEN TO USE IT. For "how many of these are in each district", "which \
neighbourhood has the most of them", "break this down by administrative \
area" — any question whose answer is a table of areas with a number against \
each. It is the only tool that reads two datasets at once, and the only way \
to group by something that is not a column but a shape.

Use `geoparquet_aggregate_attribute` instead when you can group by a column \
the dataset already carries; it is much cheaper. Use this one when the \
grouping is geographic and the boundaries live in a different file.

COST. This is the most expensive tool here, and knowingly so: the rectangle \
prunes both datasets before the join, but the containment test still has to \
decode real geometry on both sides. Expect tens of megabytes and tens of \
seconds on a city-sized box, against single-digit megabytes for the other \
tools. Keep the rectangle tight, and prefer a narrower `polygon_subtype`.

PARAMETERS.
  min_lon, min_lat, max_lon, max_lat: the rectangle, in WGS 84 degrees. All \
four are required — this tool has no whole-world mode.
  point_source: the dataset being counted.
  polygon_source: the dataset providing the containing areas. It must be a \
polygonal dataset; a point dataset is refused before anything is read.
  polygon_subtype: narrows the polygon side to one administrative level, for \
example 'locality' or 'county'. Without it a country-sized polygon is \
returned alongside a neighbourhood one, because both overlap the rectangle, \
and the counts are then not comparable to each other.
  limit: maximum polygons returned, ordered by count descending.

WHAT COMES BACK. `polygons`, each with `polygon_name`, `polygon_subtype` and \
`feature_count`; the `sql` that ran; and the `scan` block. The count is the \
number of features whose geometry is contained by that polygon, not merely \
overlapping its bounding box."""


def filter_spatial(
    source: str = engine.DEFAULT_SOURCE,
    min_lon: float | None = None, min_lat: float | None = None,
    max_lon: float | None = None, max_lat: float | None = None,
    wkt: str | None = None, columns: list[str] | None = None,
    category: str | None = None, name_contains: str | None = None,
    min_confidence: float | None = None, include_geometry: bool = True, limit: int = 50,
) -> dict[str, Any]:
    """Return features intersecting a rectangle or a WKT geometry, as GeoJSON."""
    return engine.spatial_filter(
        source=source, min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
        wkt=wkt, columns=columns, category=category, name_contains=name_contains,
        min_confidence=min_confidence, include_geometry=include_geometry, limit=limit,
        **dependencies.engine_kwargs(),
    )


def find_nearest(
    lon: float, lat: float,
    radius_km: float = 1.0,
    source: str = engine.DEFAULT_SOURCE,
    category: str | None = None, name_contains: str | None = None,
    columns: list[str] | None = None, limit: int = 20,
) -> dict[str, Any]:
    """Return the features closest to a point within a radius, nearest first."""
    return engine.nearest(
        lon=lon, lat=lat, radius_km=radius_km, source=source, category=category,
        name_contains=name_contains, columns=columns, limit=limit,
        **dependencies.engine_kwargs(),
    )


def aggregate_attribute(
    group_by: str,
    source: str = engine.DEFAULT_SOURCE,
    aggregate: str = "count", measure: str | None = None,
    min_lon: float | None = None, min_lat: float | None = None,
    max_lon: float | None = None, max_lat: float | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Group rows by one column and aggregate another, optionally inside a rectangle."""
    return engine.attribute_aggregate(
        group_by=group_by, source=source, aggregate=aggregate, measure=measure,
        min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat, limit=limit,
        **dependencies.engine_kwargs(),
    )


def summarize_h3(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float,
    resolution: int = 8,
    source: str = engine.DEFAULT_SOURCE,
    limit: int = 200, include_cell_centre: bool = True,
) -> dict[str, Any]:
    """Bin features into H3 cells and return the count per cell."""
    return engine.h3_aggregate(
        min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
        resolution=resolution, source=source, limit=limit,
        include_cell_centre=include_cell_centre, **dependencies.engine_kwargs(),
    )


def count_in_polygons(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float,
    point_source: str = engine.DEFAULT_SOURCE,
    polygon_source: str = "overture_divisions",
    polygon_subtype: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Count the features of one dataset falling inside each polygon of another."""
    return engine.point_in_polygon(
        min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
        point_source=point_source, polygon_source=polygon_source,
        polygon_subtype=polygon_subtype, limit=limit, **dependencies.engine_kwargs(),
    )


def register(server) -> None:
    server.tool(name="geoparquet_filter_spatial", description=FILTER_SPATIAL)(filter_spatial)
    server.tool(name="geoparquet_find_nearest", description=FIND_NEAREST)(find_nearest)
    server.tool(
        name="geoparquet_aggregate_attribute", description=AGGREGATE_ATTRIBUTE
    )(aggregate_attribute)
    server.tool(name="geoparquet_summarize_h3", description=SUMMARIZE_H3)(summarize_h3)
    server.tool(
        name="geoparquet_count_in_polygons", description=COUNT_IN_POLYGONS
    )(count_in_polygons)
