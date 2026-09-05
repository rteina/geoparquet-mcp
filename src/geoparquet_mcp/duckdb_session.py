"""DuckDB connection management, extension loading, and scan accounting.

Everything this server does runs through a single short-lived DuckDB
connection configured to read Parquet over HTTP. No data is ever copied into
a local database: DuckDB issues HTTP range requests against the remote file
and decodes only the byte ranges it needs.

The other half of this module is measurement. Every remote read is accounted
for by DuckDB's own HTTP log, so each tool can report how many bytes actually
crossed the network. That number is the point of the project, so it is a
first-class part of every result rather than a debugging aid.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import duckdb

# DuckDB extensions required to read remote GeoParquet. `httpfs` provides the
# HTTP/S3 file systems; `spatial` provides ST_* functions used by the tools.
REQUIRED_EXTENSIONS = ("httpfs", "spatial")

# Public object stores are read anonymously. The Overture bucket lives in
# us-west-2; DuckDB needs the region to build the correct endpoint.
DEFAULT_S3_REGION = "us-west-2"

# Ceilings. A remote scan can always be made expensive by a careless query, so
# the session caps memory, thread count and per-request patience up front.
DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4
DEFAULT_HTTP_TIMEOUT_SECONDS = 60
DEFAULT_HTTP_RETRIES = 3

# Hard cap on rows returned to the client, whatever a tool asks for.
MAX_ROW_LIMIT = 1000

_HTTP_BYTES_SQL = """
SELECT
    coalesce(sum(TRY_CAST(response.headers['Content-Length'] AS BIGINT)), 0) AS bytes,
    count(*) AS requests,
    count(DISTINCT request.url) AS files
FROM duckdb_logs_parsed('HTTP')
WHERE request.type = 'GET'
"""


@dataclass
class ScanReport:
    """What a single query actually cost on the wire."""

    bytes_scanned: int = 0
    http_requests: int = 0
    remote_files_touched: int = 0
    elapsed_ms: float = 0.0

    @property
    def megabytes_scanned(self) -> float:
        return round(self.bytes_scanned / 1_000_000, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bytes_scanned": self.bytes_scanned,
            "megabytes_scanned": self.megabytes_scanned,
            "http_requests": self.http_requests,
            "remote_files_touched": self.remote_files_touched,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


@dataclass
class SessionConfig:
    """Tunables for a DuckDB session."""

    memory_limit: str = DEFAULT_MEMORY_LIMIT
    threads: int = DEFAULT_THREADS
    s3_region: str = DEFAULT_S3_REGION
    http_timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS
    http_retries: int = DEFAULT_HTTP_RETRIES
    extensions: tuple[str, ...] = field(default=REQUIRED_EXTENSIONS)


def connect(config: SessionConfig | None = None) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection wired for remote GeoParquet.

    The connection holds no data. It is a query engine pointed at object
    storage, which is why creating one per request is cheap enough to do.
    """
    config = config or SessionConfig()
    con = duckdb.connect(database=":memory:")

    for extension in config.extensions:
        con.execute(f"INSTALL {extension}")
        con.execute(f"LOAD {extension}")

    con.execute(f"SET memory_limit='{config.memory_limit}'")
    con.execute(f"SET threads={config.threads}")
    con.execute(f"SET s3_region='{config.s3_region}'")
    con.execute(f"SET http_timeout={config.http_timeout_seconds}")
    con.execute(f"SET http_retries={config.http_retries}")

    # Enable byte accounting for the whole session; each measurement window
    # truncates the log so the numbers belong to one query only.
    con.execute("CALL enable_logging('HTTP')")
    return con


def _http_totals(con: duckdb.DuckDBPyConnection) -> tuple[int, int, int]:
    row = con.execute(_HTTP_BYTES_SQL).fetchone()
    if row is None:
        return 0, 0, 0
    return int(row[0]), int(row[1]), int(row[2])


@contextmanager
def measured(con: duckdb.DuckDBPyConnection) -> Iterator[ScanReport]:
    """Account for every byte a block of queries pulls over HTTP.

    DuckDB logs one entry per HTTP request, including the response
    Content-Length. Truncating the log first means the totals collected on
    exit describe exactly the work done inside the block.
    """
    con.execute("CALL truncate_duckdb_logs()")
    report = ScanReport()
    started = time.perf_counter()
    try:
        yield report
    finally:
        report.elapsed_ms = (time.perf_counter() - started) * 1000
        report.bytes_scanned, report.http_requests, report.remote_files_touched = _http_totals(con)


def fetch_records(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Run a query and return plain JSON-friendly dicts.

    MCP results are serialised to JSON, so the connection is drained into
    Python primitives here rather than handing back a DuckDB relation.
    """
    cursor = con.execute(sql, parameters or [])
    columns = [description[0] for description in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def clamp_limit(limit: int) -> int:
    """Keep a caller-supplied row limit inside the session ceiling."""
    return max(1, min(int(limit), MAX_ROW_LIMIT))
