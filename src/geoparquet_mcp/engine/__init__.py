"""The engine: spatial analysis over remote GeoParquet, with no protocol attached.

This package is the whole capability of the project. It opens a DuckDB
session against public object storage, resolves a closed set of datasets, runs
spatial operations inside the remote Parquet files, and reports the bytes each
one pulled over the network.

It does not import `mcp`, and nothing in it knows that an MCP server exists.
`tools/` and `resources/` are callers: they validate protocol-shaped input,
delegate here, and format what comes back. That boundary is the architectural
claim of the project, and it is checked by a test rather than by good
intentions — only `server.py` may import `mcp`.

The surface below is what a caller is meant to use. Everything else is
internal, including the SQL.
"""

from __future__ import annotations

from geoparquet_mcp.engine.errors import (
    CapabilityUnavailableError,
    EngineError,
    InvalidRequestError,
    RemoteReadError,
    ScopeViolationError,
    UnknownColumnError,
    UnknownSourceError,
)
from geoparquet_mcp.engine.operations import (
    BoundingBox,
    bbox_query,
    column_statistics,
    dataset_extent,
    dataset_schema,
    h3_aggregate,
    list_datasets,
    nearest,
    point_in_polygon,
)
from geoparquet_mcp.engine.session import (
    MAX_ROW_LIMIT,
    Measurement,
    ScanReport,
    Session,
    SessionConfig,
    get_session,
    reset_session,
)
from geoparquet_mcp.engine.sources import (
    DEFAULT_SOURCE,
    DatasetScope,
    Source,
    default_scope,
    resolve_release,
)

__all__ = [
    # Operations — the reason this package exists.
    "list_datasets",
    "dataset_schema",
    "dataset_extent",
    "bbox_query",
    "nearest",
    "column_statistics",
    "h3_aggregate",
    "point_in_polygon",
    # Inputs.
    "BoundingBox",
    # Perimeter.
    "DatasetScope",
    "Source",
    "default_scope",
    "resolve_release",
    "DEFAULT_SOURCE",
    # Session and measurement.
    "Session",
    "SessionConfig",
    "Measurement",
    "ScanReport",
    "get_session",
    "reset_session",
    "MAX_ROW_LIMIT",
    # Errors.
    "EngineError",
    "InvalidRequestError",
    "UnknownSourceError",
    "UnknownColumnError",
    "ScopeViolationError",
    "CapabilityUnavailableError",
    "RemoteReadError",
]
