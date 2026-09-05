"""Spatial operations executed inside the remote Parquet file.

Every operation here turns a spatial question into a predicate over the
dataset's `bbox` struct. That matters more than it looks: Parquet stores
per-row-group min/max statistics for `bbox.xmin`, `bbox.xmax`, `bbox.ymin`
and `bbox.ymax`, so DuckDB can decide from the footer alone which row groups
can possibly contain the answer, and never request the bytes of the others.
Combined with column projection, a city-scale question against a continental
dataset costs a few megabytes of HTTP range requests.

The rule the whole module follows: whatever the user-facing question is,
derive a rectangle from it first, because a rectangle is the only thing
Parquet statistics can prune on.

This module knows nothing about MCP. It takes primitives and Pydantic models,
returns dictionaries, and raises `geoparquet_mcp.engine.errors` types. The MCP
tool layer is one of its callers, not its owner.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from geoparquet_mcp.engine import sources
from geoparquet_mcp.engine.errors import (
    InvalidRequestError,
    RemoteReadError,
    UnknownColumnError,
)
from geoparquet_mcp.engine.session import (
    Measurement,
    Session,
    clamp_limit,
    get_session,
)
from geoparquet_mcp.engine.sources import DatasetScope, Source

EARTH_RADIUS_KM = 6371.0088

# H3 resolution 0 is continent-sized, 15 is under a square metre.
MIN_H3_RESOLUTION = 0
MAX_H3_RESOLUTION = 15

# Caller-supplied column references are interpolated into SQL, so they are
# restricted to dotted identifiers. Registry-supplied projections are trusted
# and bypass this; anything arriving from outside does not.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# `parquet_metadata()` renders a nested column's path with ", " between the
# levels, so the bbox members are addressed as 'bbox, xmin' and not 'bbox.xmin'.
# The four rectangle fields, in the order the public operations take them.
_BBOX_FIELDS = ("min_lon", "min_lat", "max_lon", "max_lat")

_BBOX_STAT_PATHS = {
    "xmin": "{column}, xmin",
    "ymin": "{column}, ymin",
    "xmax": "{column}, xmax",
    "ymax": "{column}, ymax",
}


# ---------------------------------------------------------------------------
# Validated inputs
# ---------------------------------------------------------------------------


def _safe_column(value: str) -> str:
    """Reject a column reference that is not a plain dotted identifier."""
    if not _IDENTIFIER.match(value):
        raise ValueError(
            f"{value!r} is not a valid column reference; expected a name such as "
            f"'confidence' or a nested path such as 'categories.primary'"
        )
    return value


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

    def predicate(self, column: str = "bbox", alias: str | None = None) -> str:
        """SQL for "this feature's bounding box intersects mine".

        Written as four independent comparisons on the four bbox members so
        that each one can prune row groups on its own statistics. An
        equivalent expression using a geometry function would be correct but
        opaque to the Parquet reader, and would read the whole file.
        """
        qualified = f"{alias}.{column}" if alias else column
        return (
            f"{qualified}.xmin <= {self.max_lon} AND {qualified}.xmax >= {self.min_lon} "
            f"AND {qualified}.ymin <= {self.max_lat} AND {qualified}.ymax >= {self.min_lat}"
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "min_lon": self.min_lon,
            "min_lat": self.min_lat,
            "max_lon": self.max_lon,
            "max_lat": self.max_lat,
        }


class _Filters(BaseModel):
    """Optional non-spatial narrowing shared by the feature-returning operations."""

    category: str | None = Field(default=None, description="Exact match on the category column")
    name_contains: str | None = Field(default=None, description="Case-insensitive substring")
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class BboxQueryRequest(_Filters):
    """Inputs to `bbox_query`."""

    source: str
    bbox: BoundingBox
    columns: list[str] | None = None
    include_geometry: bool = True
    limit: int = Field(default=50, ge=1)

    @field_validator("columns")
    @classmethod
    def _columns_are_identifiers(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [_safe_column(column) for column in value]


class NearestRequest(_Filters):
    """Inputs to `nearest`."""

    source: str
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    radius_km: float = Field(default=1.0, gt=0, le=500)
    columns: list[str] | None = None
    limit: int = Field(default=20, ge=1)

    @field_validator("columns")
    @classmethod
    def _columns_are_identifiers(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [_safe_column(column) for column in value]


class ColumnStatisticsRequest(BaseModel):
    """Inputs to `column_statistics`."""

    source: str
    column: str
    bbox: BoundingBox | None = None
    top_k: int = Field(default=25, ge=1, le=200)
    histogram_buckets: int = Field(default=10, ge=2, le=100)

    @field_validator("column")
    @classmethod
    def _column_is_an_identifier(cls, value: str) -> str:
        return _safe_column(value)


class H3AggregateRequest(BaseModel):
    """Inputs to `h3_aggregate`."""

    source: str
    bbox: BoundingBox
    resolution: int = Field(
        default=8,
        ge=MIN_H3_RESOLUTION,
        le=MAX_H3_RESOLUTION,
        description="H3 resolution: 0 is continent-sized, 8 is roughly a neighbourhood",
    )
    limit: int = Field(default=200, ge=1)
    include_cell_centre: bool = True


class PointInPolygonRequest(BaseModel):
    """Inputs to `point_in_polygon`."""

    point_source: str
    polygon_source: str
    bbox: BoundingBox
    polygon_subtype: str | None = None
    limit: int = Field(default=50, ge=1)


class SpatialFilterRequest(_Filters):
    """Inputs to `spatial_filter`, the general form behind `bbox_query`."""

    source: str
    bbox: BoundingBox | None = None
    wkt: str | None = Field(
        default=None,
        description="A WKT geometry in WGS 84 degrees, as an alternative to a rectangle",
    )
    columns: list[str] | None = None
    include_geometry: bool = True
    limit: int = Field(default=50, ge=1)

    @field_validator("columns")
    @classmethod
    def _columns_are_identifiers(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [_safe_column(column) for column in value]

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> SpatialFilterRequest:
        if (self.bbox is None) == (self.wkt is None):
            raise ValueError(
                "give exactly one of a bounding box (min_lon/min_lat/max_lon/max_lat) "
                "or a `wkt` geometry"
            )
        return self


class PreviewRequest(BaseModel):
    """Inputs to `preview_rows`."""

    source: str
    columns: list[str] | None = None
    limit: int = Field(default=10, ge=1, le=100)

    @field_validator("columns")
    @classmethod
    def _columns_are_identifiers(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [_safe_column(column) for column in value]


class AttributeAggregateRequest(BaseModel):
    """Inputs to `attribute_aggregate`."""

    source: str
    group_by: str = Field(description="Column whose distinct values become the groups")
    aggregate: Literal["count", "sum", "avg", "min", "max"] = "count"
    measure: str | None = Field(
        default=None, description="Column to aggregate; required for all but `count`"
    )
    bbox: BoundingBox | None = None
    limit: int = Field(default=50, ge=1)

    @field_validator("group_by", "measure")
    @classmethod
    def _columns_are_identifiers(cls, value: str | None) -> str | None:
        return None if value is None else _safe_column(value)

    @model_validator(mode="after")
    def _measure_matches_aggregate(self) -> AttributeAggregateRequest:
        if self.aggregate == "count" and self.measure is not None:
            raise ValueError("`count` counts rows and takes no `measure` column")
        if self.aggregate != "count" and self.measure is None:
            raise ValueError(f"`{self.aggregate}` needs a `measure` column to aggregate")
        return self


def _validated(model: type[BaseModel], **kwargs: Any) -> Any:
    """Build a request model, reporting failures as `InvalidRequestError`.

    Pydantic's own message is precise but shaped for a stack trace. This
    flattens it to one line per bad field, because the caller is often a model
    that will read the message and retry.
    """
    try:
        return model(**kwargs)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"]) or "(request)"
            problems.append(f"{where}: {error['msg']}")
        raise InvalidRequestError(
            f"invalid arguments for {model.__name__}: " + "; ".join(problems)
        ) from exc


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _scope_of(scope: DatasetScope | None) -> DatasetScope:
    return scope if scope is not None else sources.default_scope()


def _session_of(session: Session | None) -> Session:
    return session if session is not None else get_session()


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


def _optional_box(
    min_lon: float | None,
    min_lat: float | None,
    max_lon: float | None,
    max_lat: float | None,
) -> dict[str, float] | None:
    """A rectangle from four optional corners: all four, or none at all."""
    corners = (min_lon, min_lat, max_lon, max_lat)
    if all(value is None for value in corners):
        return None
    if any(value is None for value in corners):
        raise InvalidRequestError(
            "a bounding box needs all four of min_lon, min_lat, max_lon and max_lat; "
            "omit all four to cover the whole dataset"
        )
    return dict(zip(_BBOX_FIELDS, corners, strict=True))


def _projection(definition: Source, columns: list[str] | None) -> str:
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


def _attribute_filters(definition: Source, filters: _Filters) -> tuple[list[str], list[Any]]:
    """Optional non-spatial predicates, as SQL fragments plus bound values.

    Values are always bound, never interpolated; only column names taken from
    the source registry reach the SQL text.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if filters.category:
        if not definition.category_column:
            raise InvalidRequestError(
                f"source {definition.name!r} has no category column, so `category` "
                f"cannot be used against it"
            )
        clauses.append(f"{definition.category_column} = ?")
        params.append(filters.category)
    if filters.name_contains:
        if not definition.name_column:
            raise InvalidRequestError(
                f"source {definition.name!r} has no name column, so `name_contains` "
                f"cannot be used against it"
            )
        clauses.append(f"lower({definition.name_column}) LIKE lower(?)")
        params.append(f"%{filters.name_contains}%")
    if filters.min_confidence is not None:
        if definition.name != "overture_places":
            raise InvalidRequestError(
                f"source {definition.name!r} has no confidence column, so "
                f"`min_confidence` cannot be used against it"
            )
        clauses.append("confidence >= ?")
        params.append(filters.min_confidence)
    return clauses, params


def _column_names(measurement: Measurement, target: str) -> list[str]:
    """Top-level column names of a remote dataset, read from its footer."""
    rows = measurement.records(f"DESCRIBE SELECT * FROM read_parquet('{target}')")
    return [row["column_name"] for row in rows]


def _column_type(measurement: Measurement, target: str, column: str) -> str:
    """The DuckDB type of a column expression, resolved without reading data.

    `DESCRIBE` binds the expression against the Parquet schema and returns its
    type, so an unknown column is caught here — with the real column list in
    the message — rather than as a DuckDB binder error.
    """
    try:
        rows = measurement.records(
            f"DESCRIBE SELECT {column} AS value FROM read_parquet('{target}') LIMIT 0"
        )
    except RemoteReadError as exc:
        available = ", ".join(_column_names(measurement, target))
        raise UnknownColumnError(
            f"column {column!r} does not exist in this dataset. Available top-level "
            f"columns: {available}. Nested fields are addressed with a dot, for "
            f"example 'categories.primary'. (DuckDB said: {exc})"
        ) from exc
    return rows[0]["column_type"]


_NUMERIC_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
    "REAL",
)


def _is_numeric(duckdb_type: str) -> bool:
    return duckdb_type.upper().split("(")[0] in _NUMERIC_TYPES


def _result_envelope(definition: Source, scope: DatasetScope) -> dict[str, Any]:
    """The provenance fields every operation returns."""
    return {
        "source": definition.name,
        "release": scope.release,
        "attribution": definition.attribution,
        "license": definition.license,
    }


# ---------------------------------------------------------------------------
# 1. Catalogue
# ---------------------------------------------------------------------------


def list_datasets(
    scope: DatasetScope | None = None,
    exact: bool = False,
    session: Session | None = None,
) -> dict[str, Any]:
    """List the datasets in scope, with row count and remote size for each.

    Contract: with `exact=False` (the default) the counts come from the
    registry and nothing is read over the network beyond release resolution.
    With `exact=True` every dataset's Parquet footers are read, which is exact
    but costs one round trip per part file — cheap for a 16-part dataset,
    slow for a 513-part one. The `scan` block reports what it cost either way.
    """
    scope = _scope_of(scope)
    entries = scope.entries()

    with _session_of(session).measure() as measurement:
        for entry in entries:
            if not exact:
                entry["row_count"] = entry["approximate_rows"]
                entry["remote_bytes"] = entry["approximate_bytes"]
                entry["counts_are_exact"] = False
                continue
            footprint = measurement.one(
                f"""
                SELECT
                    count(*)             AS remote_files,
                    sum(num_rows)        AS row_count,
                    sum(file_size_bytes) AS remote_bytes,
                    sum(num_row_groups)  AS row_groups
                FROM parquet_file_metadata('{entry["scan_target"]}')
                """
            )
            entry.update(footprint)
            entry["counts_are_exact"] = True

    return {
        "release": scope.release,
        "default_source": sources.DEFAULT_SOURCE,
        "scope": scope.names,
        "counts_are_exact": exact,
        "datasets": entries,
        "scan": measurement.report.as_dict(),
    }


# ---------------------------------------------------------------------------
# 2. Schema
# ---------------------------------------------------------------------------


def dataset_schema(
    source: str = sources.DEFAULT_SOURCE,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Return one dataset's columns and types, and say which one is the geometry.

    Contract: reads Parquet footer metadata only — never a data page — so the
    cost is proportional to the number of row groups rather than to the number
    of rows. The exact row count, part count, remote size, coordinate
    reference system and geographic extent all come from those same footers.

    That is not free on a wide dataset: Overture places carries 4096 row
    groups across 16 parts, and reading their footers cold costs about 26 MB
    against a 10.5 GB file. The session caches footers, so the second call
    against a dataset costs nothing.
    """
    scope = _scope_of(scope)
    definition = scope.get(source)
    target = scope.target(source)

    with _session_of(session).measure() as measurement:
        schema = measurement.records(f"DESCRIBE SELECT * FROM read_parquet('{target}')")
        footprint = measurement.one(
            f"""
            SELECT
                count(*)             AS remote_files,
                sum(num_rows)        AS row_count,
                sum(file_size_bytes) AS remote_bytes,
                sum(num_row_groups)  AS row_groups
            FROM parquet_file_metadata('{target}')
            """
        )
        geo = _geoparquet_metadata(measurement, target)
        bounds = _extent_from_statistics(measurement, target, definition.bbox_column)

    extent = (
        {key: bounds[key] for key in _BBOX_FIELDS} if bounds["min_lon"] is not None else None
    )

    return {
        **_result_envelope(definition, scope),
        "title": definition.title,
        "description": definition.description,
        "scan_target": target,
        "https_prefix": definition.https_prefix(scope.release),
        "columns": [
            {
                "name": row["column_name"],
                "type": row["column_type"],
                "role": _column_role(definition, row["column_name"]),
            }
            for row in schema
        ],
        "geometry_column": definition.geometry_column,
        "bbox_column": definition.bbox_column,
        "name_column": definition.name_column,
        "category_column": definition.category_column,
        "default_columns": list(definition.default_columns),
        **geo,
        "extent": extent,
        **footprint,
        "notes": definition.notes,
        "scan": measurement.report.as_dict(),
    }


def _column_role(definition: Source, name: str) -> str | None:
    if name == definition.geometry_column:
        return "geometry"
    if name == definition.bbox_column:
        return "bbox"
    if definition.name_column and definition.name_column.split(".")[0] == name:
        return "name"
    if definition.category_column and definition.category_column.split(".")[0] == name:
        return "category"
    return None


# ---------------------------------------------------------------------------
# 3. Extent
# ---------------------------------------------------------------------------


def _extent_from_statistics(
    measurement: Measurement, target: str, column: str
) -> dict[str, Any]:
    """A dataset's bounding box, from the row-group statistics in its footers.

    Returns `min_lon` as None when the file carries no statistics on its bbox
    struct, which is the signal that the extent cannot be had cheaply.
    """
    paths = {key: pattern.format(column=column) for key, pattern in _BBOX_STAT_PATHS.items()}
    return measurement.one(
        f"""
        SELECT
            min(TRY_CAST(stats_min AS DOUBLE))
                FILTER (WHERE path_in_schema = '{paths["xmin"]}') AS min_lon,
            min(TRY_CAST(stats_min AS DOUBLE))
                FILTER (WHERE path_in_schema = '{paths["ymin"]}') AS min_lat,
            max(TRY_CAST(stats_max AS DOUBLE))
                FILTER (WHERE path_in_schema = '{paths["xmax"]}') AS max_lon,
            max(TRY_CAST(stats_max AS DOUBLE))
                FILTER (WHERE path_in_schema = '{paths["ymax"]}') AS max_lat,
            count(*) FILTER (WHERE path_in_schema = '{paths["xmin"]}') AS row_groups
        FROM parquet_metadata('{target}')
        """
    )


def _geoparquet_metadata(measurement: Measurement, target: str) -> dict[str, Any]:
    """The GeoParquet `geo` key from the file's key/value metadata.

    GeoParquet records the CRS, the encoding and the geometry types in a JSON
    document stored under the `geo` key in the Parquet footer, not in the
    Arrow schema. It is the authoritative answer to "what CRS is this?", and
    reading it costs nothing beyond the footer already being fetched.

    Per the specification a null `crs` means OGC:CRS84 — longitude/latitude in
    WGS 84 degrees — which is what every operation here assumes.
    """
    rows = measurement.records(
        f"""
        SELECT decode(value) AS document
        FROM parquet_kv_metadata('{target}')
        WHERE decode(key) = 'geo'
        LIMIT 1
        """
    )
    if not rows:
        return {"crs": None, "encoding": None, "geometry_types": [], "geoparquet_version": None}
    document = json.loads(rows[0]["document"])
    primary = document.get("primary_column")
    column = (document.get("columns") or {}).get(primary) or {}
    crs = column.get("crs")
    return {
        "crs": _crs_identifier(crs),
        "crs_is_default": crs is None,
        "encoding": column.get("encoding"),
        "geometry_types": column.get("geometry_types") or [],
        "geoparquet_version": document.get("version"),
    }


def _crs_identifier(crs: Any) -> str:
    """A readable CRS name from GeoParquet's PROJJSON, or the spec's default."""
    if crs is None:
        return "OGC:CRS84"
    if isinstance(crs, str):
        return crs
    identifier = crs.get("id") or {}
    authority, code = identifier.get("authority"), identifier.get("code")
    if authority and code is not None:
        return f"{authority}:{code}"
    return crs.get("name") or "unknown"


def dataset_extent(
    source: str = sources.DEFAULT_SOURCE,
    scope: DatasetScope | None = None,
    allow_scan_fallback: bool = False,
    session: Session | None = None,
) -> dict[str, Any]:
    """Return the bounding box of a whole dataset, from Parquet statistics.

    Contract: the extent is computed from the per-row-group min/max statistics
    of the four `bbox` members recorded in the file footers. No geometry is
    decoded and no data page is fetched, so the cost is proportional to the
    number of row groups, not to the number of rows.

    This only works when the dataset carries a bbox struct with statistics —
    true for Overture, not true for every GeoParquet file in the wild. When
    the statistics are absent the extent cannot be had cheaply: this raises
    `RemoteReadError` unless `allow_scan_fallback=True`, in which case it
    falls back to aggregating the bbox columns, which reads those columns in
    full across the dataset and is expensive by construction.
    """
    scope = _scope_of(scope)
    definition = scope.get(source)
    target = scope.target(source)
    column = definition.bbox_column

    with _session_of(session).measure() as measurement:
        stats = _extent_from_statistics(measurement, target, column)
        from_statistics = stats["min_lon"] is not None

        if not from_statistics:
            if not allow_scan_fallback:
                raise RemoteReadError(
                    f"dataset {source!r} has no Parquet statistics on its "
                    f"{column!r} struct, so its extent cannot be read from the "
                    f"footers. Pass allow_scan_fallback=True to compute it by "
                    f"scanning the bbox columns instead — that reads the whole "
                    f"dataset's bbox columns and is slow by design."
                )
            stats = measurement.one(
                f"""
                SELECT min({column}.xmin) AS min_lon, min({column}.ymin) AS min_lat,
                       max({column}.xmax) AS max_lon, max({column}.ymax) AS max_lat,
                       count(*) AS row_groups
                FROM read_parquet('{target}')
                """
            )

    return {
        **_result_envelope(definition, scope),
        "extent": {
            "min_lon": stats["min_lon"],
            "min_lat": stats["min_lat"],
            "max_lon": stats["max_lon"],
            "max_lat": stats["max_lat"],
        },
        "from_statistics": from_statistics,
        "row_groups_examined": stats["row_groups"],
        "scan": measurement.report.as_dict(),
    }


# ---------------------------------------------------------------------------
# 4. Spatial query
# ---------------------------------------------------------------------------


def _wkt_envelope(measurement: Measurement, wkt: str) -> BoundingBox:
    """The bounding rectangle of a WKT geometry, computed locally.

    A WKT polygon is not something Parquet statistics can prune on, but its
    envelope is. This resolves the envelope with a constant-folded query that
    touches no remote file, so the pruning rectangle costs zero bytes.
    """
    try:
        corners = measurement.one(
            "SELECT ST_XMin(g) AS min_lon, ST_YMin(g) AS min_lat, "
            "ST_XMax(g) AS max_lon, ST_YMax(g) AS max_lat "
            "FROM (SELECT ST_GeomFromText(?) AS g)",
            [wkt],
        )
    except RemoteReadError as exc:
        raise InvalidRequestError(
            f"could not parse `wkt` as a WKT geometry: {exc}. Expected something like "
            f"'POLYGON ((2.33 48.85, 2.36 48.85, 2.36 48.87, 2.33 48.87, 2.33 48.85))'."
        ) from exc
    if corners["min_lon"] is None:
        raise InvalidRequestError("`wkt` parsed to an empty geometry, which selects nothing")
    return _validated(BoundingBox, **corners)


def spatial_filter(
    source: str = sources.DEFAULT_SOURCE,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    wkt: str | None = None,
    category: str | None = None,
    name_contains: str | None = None,
    min_confidence: float | None = None,
    columns: list[str] | None = None,
    include_geometry: bool = True,
    limit: int = 50,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Return features intersecting a rectangle or a WKT geometry, as GeoJSON.

    Contract: whichever shape is given, a rectangle is what reaches the
    Parquet reader. A bounding box is pushed down directly; a WKT geometry is
    reduced to its envelope for pruning and then re-tested exactly with
    `ST_Intersects` over the surviving rows, so the answer is exact but the
    read is still proportional to the envelope.

    Only the byte ranges that can contain a match are fetched, and the `scan`
    block reports how many bytes that was. The result is a GeoJSON
    FeatureCollection whose `properties` carry the selected columns.

    `include_geometry=False` skips reading the geometry column and synthesises
    a Point from the feature's stored bounding-box corner instead. That is
    exact for point datasets and an approximation for polygonal ones, but it
    avoids fetching the single widest column in the file. It has no effect on
    the read when `wkt` is used, because the exact test needs the geometry.
    """
    if wkt is None and any(value is None for value in (min_lon, min_lat, max_lon, max_lat)):
        raise InvalidRequestError(
            "a rectangle needs all four of min_lon, min_lat, max_lon and max_lat; "
            "pass `wkt` instead to filter on an arbitrary geometry"
        )
    box = _optional_box(min_lon, min_lat, max_lon, max_lat)

    request: SpatialFilterRequest = _validated(
        SpatialFilterRequest,
        source=source,
        bbox=box,
        wkt=wkt,
        category=category,
        name_contains=name_contains,
        min_confidence=min_confidence,
        columns=columns,
        include_geometry=include_geometry,
        limit=limit,
    )
    scope = _scope_of(scope)
    definition = scope.get(request.source)
    target = scope.target(request.source)
    row_limit = clamp_limit(request.limit)

    with _session_of(session).measure() as measurement:
        pruning_box = request.bbox or _wkt_envelope(measurement, request.wkt or "")

        clauses: list[str] = []
        params: list[Any] = []
        if request.wkt is not None:
            # Exactness, applied only to the rows the envelope let through.
            clauses.append(f"ST_Intersects({definition.geometry_column}, ST_GeomFromText(?))")
            params.append(request.wkt)
        attribute_clauses, attribute_params = _attribute_filters(definition, request)
        clauses.extend(attribute_clauses)
        params.extend(attribute_params)
        where = " AND ".join([pruning_box.predicate(definition.bbox_column), *clauses])

        if request.include_geometry or request.wkt is not None:
            geometry_sql = f"ST_AsGeoJSON({definition.geometry_column})"
        else:
            # The bbox corner, which for a point dataset *is* the point.
            geometry_sql = (
                f"json_object('type', 'Point', 'coordinates', "
                f"json_array({definition.bbox_column}.xmin, {definition.bbox_column}.ymin))"
            )

        sql = (
            f"SELECT {_projection(definition, request.columns)}, "
            f"{geometry_sql} AS __geometry "
            f"FROM read_parquet('{target}') "
            f"WHERE {where} "
            f"LIMIT {row_limit}"
        )
        rows = measurement.records(sql, params)

    return {
        **_result_envelope(definition, scope),
        "bbox": pruning_box.as_dict(),
        "wkt": request.wkt,
        "geojson": _feature_collection(rows, definition),
        "feature_count": len(rows),
        "limit": row_limit,
        "truncated": len(rows) == row_limit,
        "geometry_is_exact": request.include_geometry or request.wkt is not None,
        "sql": sql,
        "scan": measurement.report.as_dict(),
    }


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
    include_geometry: bool = True,
    limit: int = 50,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Return features intersecting a lon/lat rectangle, as GeoJSON.

    The rectangle-only form of `spatial_filter`, kept because a rectangle is
    the case that prunes best and the one most callers want.
    """
    return spatial_filter(
        source=source,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        category=category,
        name_contains=name_contains,
        min_confidence=min_confidence,
        columns=columns,
        include_geometry=include_geometry,
        limit=limit,
        scope=scope,
        session=session,
    )


def _feature_collection(rows: list[dict[str, Any]], definition: Source) -> dict[str, Any]:
    """Turn measured rows into a GeoJSON FeatureCollection.

    The geometry arrives as a JSON string from `ST_AsGeoJSON`; everything else
    in the row becomes a property. `id` is lifted to the Feature id when the
    dataset has one, because GeoJSON consumers expect it there.
    """
    features = []
    for row in rows:
        properties = dict(row)
        raw_geometry = properties.pop("__geometry", None)
        geometry = json.loads(raw_geometry) if isinstance(raw_geometry, str) else raw_geometry
        feature: dict[str, Any] = {
            "type": "Feature",
            "geometry": geometry,
            "properties": properties,
        }
        if "id" in properties:
            feature["id"] = properties["id"]
        features.append(feature)
    return {
        "type": "FeatureCollection",
        "features": features,
        "attribution": definition.attribution,
    }


# ---------------------------------------------------------------------------
# 5. Nearest neighbours
# ---------------------------------------------------------------------------


def nearest(
    lon: float,
    lat: float,
    radius_km: float = 1.0,
    source: str = sources.DEFAULT_SOURCE,
    category: str | None = None,
    name_contains: str | None = None,
    columns: list[str] | None = None,
    limit: int = 20,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Return the closest features to a point, nearest first, with distances.

    Contract: a radius is not something Parquet statistics understand, so the
    search circle is first widened to its bounding rectangle, which *is*
    prunable. The exact great-circle distance is then computed only over the
    rows that survive, and used both to filter and to order. The result is a
    ranked list rather than a FeatureCollection, because the ranking and the
    distance are the answer.
    """
    request: NearestRequest = _validated(
        NearestRequest,
        source=source,
        lon=lon,
        lat=lat,
        radius_km=radius_km,
        category=category,
        name_contains=name_contains,
        columns=columns,
        limit=limit,
    )
    scope = _scope_of(scope)
    definition = scope.get(request.source)
    target = scope.target(request.source)
    box = _bbox_around(request.lon, request.lat, request.radius_km)
    row_limit = clamp_limit(request.limit)

    clauses, params = _attribute_filters(definition, request)
    distance = (
        f"ST_Distance_Sphere({definition.geometry_column}, "
        f"ST_Point({request.lon}, {request.lat})) / 1000.0"
    )
    where = " AND ".join([box.predicate(definition.bbox_column), *clauses])
    # The distance test lives in an outer query so that the inner scan carries
    # only the prunable bbox predicate; DuckDB still pushes the inner WHERE
    # into the Parquet reader.
    sql = (
        f"SELECT * FROM ("
        f"SELECT {_projection(definition, request.columns)}, "
        f"round({distance}, 4) AS distance_km "
        f"FROM read_parquet('{target}') "
        f"WHERE {where}"
        f") WHERE distance_km <= {request.radius_km} "
        f"ORDER BY distance_km "
        f"LIMIT {row_limit}"
    )

    with _session_of(session).measure() as measurement:
        rows = measurement.records(sql, params)

    return {
        **_result_envelope(definition, scope),
        "center": {"lon": request.lon, "lat": request.lat},
        "radius_km": request.radius_km,
        "search_bbox": box.as_dict(),
        "row_count": len(rows),
        "rows": rows,
        "sql": sql,
        "scan": measurement.report.as_dict(),
    }


# ---------------------------------------------------------------------------
# 6. Column statistics
# ---------------------------------------------------------------------------


def column_statistics(
    column: str,
    source: str = sources.DEFAULT_SOURCE,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    top_k: int = 25,
    histogram_buckets: int = 10,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Summarise one column: how many rows, what range, how the values spread.

    Contract: the column's type decides the shape of the answer. A numeric
    column returns count, null count, min, max, mean, standard deviation, the
    quartiles and an equal-width histogram. A categorical column returns the
    distinct-value count and the `top_k` most frequent values with their
    share. Either way the answer is a handful of rows, so the aggregate is
    computed remotely and only the summary crosses the network.

    Supplying a bounding box restricts the summary to that rectangle and makes
    the read cheap; omitting it summarises the whole dataset, which reads that
    column in full and is slow on a multi-gigabyte source.
    """
    box = None
    if any(value is not None for value in (min_lon, min_lat, max_lon, max_lat)):
        if None in (min_lon, min_lat, max_lon, max_lat):
            raise InvalidRequestError(
                "a bounding box needs all four of min_lon, min_lat, max_lon and max_lat; "
                "omit all four to summarise the whole dataset"
            )
        box = {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}

    request: ColumnStatisticsRequest = _validated(
        ColumnStatisticsRequest,
        source=source,
        column=column,
        bbox=box,
        top_k=top_k,
        histogram_buckets=histogram_buckets,
    )
    scope = _scope_of(scope)
    definition = scope.get(request.source)
    target = scope.target(request.source)
    where = f"WHERE {request.bbox.predicate(definition.bbox_column)}" if request.bbox else ""
    expression = request.column

    with _session_of(session).measure() as measurement:
        column_type = _column_type(measurement, target, expression)
        numeric = _is_numeric(column_type)

        headline = measurement.one(
            f"""
            SELECT count(*) AS row_count,
                   count({expression}) AS non_null_count,
                   count(DISTINCT {expression}) AS distinct_values
            FROM read_parquet('{target}') {where}
            """
        )

        if numeric:
            summary = measurement.one(
                f"""
                SELECT min({expression}) AS minimum,
                       max({expression}) AS maximum,
                       avg({expression}) AS mean,
                       stddev_samp({expression}) AS stddev,
                       quantile_cont({expression}, 0.25) AS p25,
                       quantile_cont({expression}, 0.50) AS median,
                       quantile_cont({expression}, 0.75) AS p75
                FROM read_parquet('{target}') {where}
                """
            )
            distribution = _numeric_histogram(
                measurement,
                target,
                where,
                expression,
                summary["minimum"],
                summary["maximum"],
                request.histogram_buckets,
            )
        else:
            summary = measurement.one(
                f"""
                SELECT min({expression})::VARCHAR AS minimum,
                       max({expression})::VARCHAR AS maximum
                FROM read_parquet('{target}') {where}
                """
            )
            distribution = measurement.records(
                f"""
                SELECT {expression}::VARCHAR AS value, count(*) AS count
                FROM read_parquet('{target}') {where}
                GROUP BY 1 ORDER BY count DESC, value
                LIMIT {request.top_k}
                """
            )

    total = headline["row_count"] or 0
    for entry in distribution:
        entry["share"] = round(entry["count"] / total, 6) if total else 0.0

    return {
        **_result_envelope(definition, scope),
        "column": expression,
        "column_type": column_type,
        "kind": "numeric" if numeric else "categorical",
        "bbox": request.bbox.as_dict() if request.bbox else None,
        "row_count": total,
        "non_null_count": headline["non_null_count"],
        "null_count": total - (headline["non_null_count"] or 0),
        "distinct_values": headline["distinct_values"],
        "summary": summary,
        "distribution": distribution,
        "scan": measurement.report.as_dict(),
    }


def _numeric_histogram(
    measurement: Measurement,
    target: str,
    where: str,
    expression: str,
    minimum: float | None,
    maximum: float | None,
    buckets: int,
) -> list[dict[str, Any]]:
    """Equal-width buckets over a numeric column's observed range."""
    if minimum is None or maximum is None or maximum <= minimum:
        return []
    width = (maximum - minimum) / buckets
    return measurement.records(
        f"""
        SELECT
            {minimum} + bucket * {width} AS lower_bound,
            {minimum} + (bucket + 1) * {width} AS upper_bound,
            bucket_count AS count
        FROM (
            SELECT least(floor(({expression} - {minimum}) / {width}), {buckets - 1}) AS bucket,
                   count(*) AS bucket_count
            FROM read_parquet('{target}') {where}
            {"AND" if where else "WHERE"} {expression} IS NOT NULL
            GROUP BY 1
        )
        ORDER BY lower_bound
        """
    )


# ---------------------------------------------------------------------------
# 7. H3 aggregation
# ---------------------------------------------------------------------------


def h3_aggregate(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    resolution: int = 8,
    source: str = sources.DEFAULT_SOURCE,
    limit: int = 200,
    include_cell_centre: bool = True,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Bin features into H3 cells and return the count per cell.

    Contract: the rectangle prunes row groups first, then each surviving
    feature is assigned to the H3 cell containing its bounding-box centre and
    the counts are aggregated remotely. Only the per-cell counts cross the
    network. Cell ids are returned as the canonical hexadecimal strings.

    Resolution is the H3 level, 0 (continent-sized) to 15 (sub-metre); 8 is
    roughly a neighbourhood and 9 roughly a block.

    Binning on the bounding-box centre rather than on the true geometry is
    deliberate: it avoids fetching the geometry column, which is the widest in
    the file. For a point dataset the two are identical; for a polygonal one
    the cell is the polygon's envelope centre.

    Verified on DuckDB 1.5.5 with the community `h3` extension. If that
    extension cannot be installed, this raises `CapabilityUnavailableError`
    rather than falling back to a Python implementation — a Python fallback
    would have to pull every matching row over the network, which would
    contradict everything else in this module.
    """
    request: H3AggregateRequest = _validated(
        H3AggregateRequest,
        source=source,
        bbox={"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat},
        resolution=resolution,
        limit=limit,
        include_cell_centre=include_cell_centre,
    )
    scope = _scope_of(scope)
    definition = scope.get(request.source)
    target = scope.target(request.source)
    row_limit = clamp_limit(request.limit)
    active = _session_of(session)
    active.require_extension("h3")

    bbox = definition.bbox_column
    cell = (
        f"h3_latlng_to_cell(({bbox}.ymin + {bbox}.ymax) / 2, "
        f"({bbox}.xmin + {bbox}.xmax) / 2, {request.resolution})"
    )
    centre = (
        ", h3_cell_to_lat(cell) AS centre_lat, h3_cell_to_lng(cell) AS centre_lon"
        if request.include_cell_centre
        else ""
    )
    sql = (
        f"SELECT h3_h3_to_string(cell) AS h3_cell{centre}, feature_count "
        f"FROM ("
        f"SELECT {cell} AS cell, count(*) AS feature_count "
        f"FROM read_parquet('{target}') "
        f"WHERE {request.bbox.predicate(bbox)} "
        f"GROUP BY 1"
        f") ORDER BY feature_count DESC, h3_cell "
        f"LIMIT {row_limit}"
    )

    with active.measure() as measurement:
        cells = measurement.records(sql)

    return {
        **_result_envelope(definition, scope),
        "bbox": request.bbox.as_dict(),
        "resolution": request.resolution,
        "cell_count": len(cells),
        "features_binned": sum(row["feature_count"] for row in cells),
        "truncated": len(cells) == row_limit,
        "cells": cells,
        "sql": sql,
        "scan": measurement.report.as_dict(),
    }


# ---------------------------------------------------------------------------
# 8. Point in polygon
# ---------------------------------------------------------------------------


def point_in_polygon(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    point_source: str = sources.DEFAULT_SOURCE,
    polygon_source: str = "overture_divisions",
    polygon_subtype: str | None = None,
    limit: int = 50,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Count the features of one dataset that fall inside each polygon of another.

    Contract: both datasets must be in scope, and the polygon side must be a
    polygonal source. The same rectangle prunes both scans before the join, so
    the containment test — the expensive part — runs only over features that
    could possibly match. The answer is one row per polygon with the number of
    points inside it, ordered by that count.

    Polygons whose envelope merely overlaps the rectangle are included, which
    means a country-level polygon shows up alongside a neighbourhood one.
    `polygon_subtype` narrows the polygon side to one administrative level.
    """
    request: PointInPolygonRequest = _validated(
        PointInPolygonRequest,
        point_source=point_source,
        polygon_source=polygon_source,
        bbox={"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat},
        polygon_subtype=polygon_subtype,
        limit=limit,
    )
    scope = _scope_of(scope)
    points = scope.get(request.point_source)
    polygons = scope.get(request.polygon_source)
    if not polygons.polygonal:
        raise InvalidRequestError(
            f"source {polygons.name!r} does not hold polygons, so it cannot be the "
            f"containing side of a point-in-polygon join; polygonal sources in scope: "
            f"{', '.join(sorted(n for n in scope.names if scope.get(n).polygonal)) or 'none'}"
        )

    row_limit = clamp_limit(request.limit)
    params: list[Any] = []
    polygon_where = request.bbox.predicate(polygons.bbox_column)
    if request.polygon_subtype:
        if not polygons.category_column:
            raise InvalidRequestError(
                f"source {polygons.name!r} has no subtype column to filter on"
            )
        polygon_where += f" AND {polygons.category_column} = ?"
        params.append(request.polygon_subtype)

    sql = (
        f"WITH polygon AS ("
        f"  SELECT id, {polygons.name_column} AS polygon_name, "
        f"         {polygons.category_column or 'NULL'} AS polygon_subtype, "
        f"         {polygons.geometry_column} AS geometry "
        f"  FROM read_parquet('{scope.target(request.polygon_source)}') "
        f"  WHERE {polygon_where}"
        f"), point AS ("
        f"  SELECT {points.geometry_column} AS geometry "
        f"  FROM read_parquet('{scope.target(request.point_source)}') "
        f"  WHERE {request.bbox.predicate(points.bbox_column)}"
        f") "
        f"SELECT polygon.polygon_name, polygon.polygon_subtype, count(*) AS feature_count "
        f"FROM point JOIN polygon ON ST_Contains(polygon.geometry, point.geometry) "
        f"GROUP BY 1, 2 ORDER BY feature_count DESC "
        f"LIMIT {row_limit}"
    )

    with _session_of(session).measure() as measurement:
        rows = measurement.records(sql, params)

    return {
        "point_source": points.name,
        "polygon_source": polygons.name,
        "release": scope.release,
        "attribution": f"{points.attribution} / {polygons.attribution}",
        "bbox": request.bbox.as_dict(),
        "polygon_subtype": request.polygon_subtype,
        "polygon_count": len(rows),
        "polygons": rows,
        "sql": sql,
        "scan": measurement.report.as_dict(),
    }


# ---------------------------------------------------------------------------
# 9. Preview
# ---------------------------------------------------------------------------


def preview_rows(
    source: str = sources.DEFAULT_SOURCE,
    columns: list[str] | None = None,
    limit: int = 10,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Return the first few rows of a dataset, to see what the values look like.

    Contract: a bare `LIMIT` with no filter, which DuckDB satisfies from the
    first row group of the first part file and then stops. The cost is one
    row group's worth of the projected columns, not one row group per part.

    This is a shape-of-the-data question, not a spatial one: the rows are
    whatever the file happens to store first, in no meaningful geographic
    order. Use it to learn how a column is actually populated — what a
    category string looks like, whether a field is mostly null — before
    writing a filter against it. Use a spatial operation to ask where things
    are.
    """
    request: PreviewRequest = _validated(
        PreviewRequest, source=source, columns=columns, limit=limit
    )
    scope = _scope_of(scope)
    definition = scope.get(request.source)
    target = scope.target(request.source)

    sql = (
        f"SELECT {_projection(definition, request.columns)} "
        f"FROM read_parquet('{target}') "
        f"LIMIT {request.limit}"
    )

    with _session_of(session).measure() as measurement:
        rows = measurement.records(sql)

    return {
        **_result_envelope(definition, scope),
        "row_count": len(rows),
        "rows": rows,
        "columns_returned": list(rows[0]) if rows else [],
        "ordering": "file order — not geographic, not ranked",
        "sql": sql,
        "scan": measurement.report.as_dict(),
    }


# ---------------------------------------------------------------------------
# 10. Attribute aggregation
# ---------------------------------------------------------------------------


def attribute_aggregate(
    group_by: str,
    source: str = sources.DEFAULT_SOURCE,
    aggregate: str = "count",
    measure: str | None = None,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    limit: int = 50,
    scope: DatasetScope | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Group rows by one column and aggregate another: GROUP BY over remote Parquet.

    Contract: the grouping and the aggregate both run inside the remote file.
    Only the group rows cross the network, so "how many places of each
    category are in this district" costs kilobytes whatever the district
    holds.

    Supplying a bounding box restricts the aggregate to that rectangle and
    makes the read cheap by pruning row groups; omitting it aggregates the
    whole dataset, which reads the grouped and measured columns in full and is
    slow by construction on a multi-gigabyte source.

    `sum`, `avg`, `min` and `max` need a `measure` column and reject a
    non-numeric one before any byte is fetched; `count` counts rows and takes
    no measure.
    """
    box = _optional_box(min_lon, min_lat, max_lon, max_lat)
    request: AttributeAggregateRequest = _validated(
        AttributeAggregateRequest,
        source=source,
        group_by=group_by,
        aggregate=aggregate,
        measure=measure,
        bbox=box,
        limit=limit,
    )
    scope = _scope_of(scope)
    definition = scope.get(request.source)
    target = scope.target(request.source)
    row_limit = clamp_limit(request.limit)
    where = f"WHERE {request.bbox.predicate(definition.bbox_column)}" if request.bbox else ""

    with _session_of(session).measure() as measurement:
        _column_type(measurement, target, request.group_by)
        if request.measure is not None:
            measure_type = _column_type(measurement, target, request.measure)
            if not _is_numeric(measure_type):
                raise InvalidRequestError(
                    f"`{request.aggregate}` needs a numeric measure, but "
                    f"{request.measure!r} is {measure_type}. Use aggregate='count' to "
                    f"count rows per group instead."
                )
        expression = (
            "count(*)" if request.measure is None
            else f"{request.aggregate}({request.measure})"
        )
        sql = (
            f"SELECT {request.group_by} AS group_value, "
            f"{expression} AS value, count(*) AS row_count "
            f"FROM read_parquet('{target}') {where} "
            f"GROUP BY 1 ORDER BY value DESC NULLS LAST, group_value "
            f"LIMIT {row_limit}"
        )
        groups = measurement.records(sql)

    return {
        **_result_envelope(definition, scope),
        "group_by": request.group_by,
        "aggregate": request.aggregate,
        "measure": request.measure,
        "bbox": request.bbox.as_dict() if request.bbox else None,
        "group_count": len(groups),
        "rows_aggregated": sum(row["row_count"] for row in groups),
        "truncated": len(groups) == row_limit,
        "groups": groups,
        "sql": sql,
        "scan": measurement.report.as_dict(),
    }
