"""DuckDB connection management, extension loading, and scan accounting.

Everything the engine does runs through one process-wide DuckDB connection
configured to read Parquet over HTTP. No data is ever copied into a local
database: DuckDB issues HTTP range requests against the remote file and
decodes only the byte ranges it needs.

The other half of this module is measurement. Every remote read is accounted
for by DuckDB's own HTTP log, so each operation can report how many bytes
actually crossed the network. That number is the point of the project, so it
is a first-class part of every result rather than a debugging aid.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import duckdb

from geoparquet_mcp.engine.errors import CapabilityUnavailableError, RemoteReadError

# DuckDB extensions required to read remote GeoParquet. `httpfs` provides the
# HTTP/S3 file systems; `spatial` provides the ST_* functions.
REQUIRED_EXTENSIONS = ("httpfs", "spatial")

# Optional extensions. Their absence disables one operation each rather than
# failing the session, so a machine without community extensions still runs
# everything else.
COMMUNITY_EXTENSIONS = {"h3": "community"}

# Public object stores are read anonymously. The Overture bucket lives in
# us-west-2; DuckDB needs the region to build the correct endpoint.
DEFAULT_S3_REGION = "us-west-2"

# Ceilings. A remote scan can always be made expensive by a careless query, so
# the session caps memory, thread count and per-request patience up front.
DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4
DEFAULT_HTTP_TIMEOUT_SECONDS = 60
DEFAULT_HTTP_RETRIES = 3

# Hard cap on rows returned to the client, whatever an operation asks for.
MAX_ROW_LIMIT = 1000

# Bytes attributable to one measurement window: GET responses logged against
# this connection, after this measurement's watermark. See `Session.measure`.
_HTTP_BYTES_SQL = """
SELECT
    coalesce(sum(TRY_CAST(response.headers['Content-Length'] AS BIGINT)), 0) AS bytes,
    count(*) AS requests,
    count(DISTINCT request.url) AS files
FROM duckdb_logs_parsed('HTTP')
WHERE request.type = 'GET'
  AND connection_id = ?
  AND query_id > ?
"""


@dataclass
class ScanReport:
    """What one measurement window actually cost on the wire."""

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
    # Parquet footers are re-read on every query without this. Since the
    # engine is long-lived and the remote files are immutable for the life of
    # a release, caching them turns the second query against a dataset into a
    # data-only read.
    http_metadata_cache: bool = True
    # DuckDB also caches decoded data pages in memory. Left on because the
    # engine is a server; the benchmark measures on cold sessions instead of
    # turning it off, so the numbers stay representative of a real first hit.
    external_file_cache: bool = True


class Measurement:
    """A measured window: an isolated cursor plus the bytes it pulled.

    Every query issued through this object runs on one private DuckDB cursor,
    and the report on exit describes exactly those queries.
    """

    def __init__(self, cursor: duckdb.DuckDBPyConnection, report: ScanReport) -> None:
        self._cursor = cursor
        self.report = report

    def execute(self, sql: str, parameters: list[Any] | None = None) -> duckdb.DuckDBPyConnection:
        """Run a statement on the measured cursor, translating DuckDB failures."""
        try:
            return self._cursor.execute(sql, parameters or [])
        except duckdb.Error as exc:
            raise RemoteReadError(_explain_duckdb_error(exc), sql=sql) from exc

    def records(self, sql: str, parameters: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run a query and return plain JSON-friendly dicts.

        Results are serialised to JSON by the callers, so the cursor is
        drained into Python primitives here rather than handing back a
        DuckDB relation.
        """
        cursor = self.execute(sql, parameters)
        columns = [description[0] for description in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def one(self, sql: str, parameters: list[Any] | None = None) -> dict[str, Any]:
        """Run a query expected to return exactly one row."""
        rows = self.records(sql, parameters)
        if not rows:
            raise RemoteReadError("query returned no rows where one was expected", sql=sql)
        return rows[0]

    @contextmanager
    def option(self, name: str, value: str) -> Iterator[None]:
        """Temporarily set a DuckDB option on this cursor only.

        Used by the benchmark to disable filter pushdown for one query
        without disturbing anything else running on the engine.
        """
        previous = self._cursor.execute(f"SELECT current_setting('{name}')").fetchone()
        restore = previous[0] if previous else ""
        self._cursor.execute(f"SET {name}='{value}'")
        try:
            yield
        finally:
            self._cursor.execute(f"SET {name}='{restore}'")


class Session:
    """A long-lived DuckDB engine pointed at remote object storage.

    One instance per process. It holds no data: creating it costs an
    in-memory database and two extension loads, and everything after that is
    HTTP range requests against files that stay where they are published.

    Thread safety
    -------------
    DuckDB allows concurrent queries on one database through `cursor()`, and
    every measurement takes its own cursor. That matters because measurement
    is the fragile part: DuckDB's HTTP log is one table for the whole
    database instance, so the obvious implementation — truncate the log, run
    the query, sum the log — silently steals bytes between concurrent
    queries.

    This session attributes instead of truncating. Each `measure()` window
    records its cursor's `current_connection_id()` and, as a watermark, its
    `current_query_id()`, then sums only the GET responses logged against
    that connection after that watermark. Two measurements running at once
    therefore cannot see each other's bytes, and a cursor reused for a second
    window does not re-count the first. The log is truncated only when no
    measurement is in flight, purely to bound its growth.
    """

    def __init__(self, config: SessionConfig | None = None) -> None:
        self.config = config or SessionConfig()
        self._connection = self._open()
        self._optional: dict[str, bool] = {}
        # Guards the in-flight counter and the truncation decision, not the
        # queries themselves — those run concurrently on their own cursors.
        self._lock = threading.Lock()
        self._in_flight = 0

    def _open(self) -> duckdb.DuckDBPyConnection:
        config = self.config
        con = duckdb.connect(database=":memory:")
        for extension in config.extensions:
            con.execute(f"INSTALL {extension}")
            con.execute(f"LOAD {extension}")
        self._apply_settings(con)
        # Enable byte accounting for the whole session. Windows are carved out
        # of the log by connection id and query watermark, never by truncation.
        con.execute("CALL enable_logging('HTTP')")
        return con

    def _apply_settings(self, con: duckdb.DuckDBPyConnection) -> None:
        config = self.config
        con.execute(f"SET memory_limit='{config.memory_limit}'")
        con.execute(f"SET threads={config.threads}")
        con.execute(f"SET s3_region='{config.s3_region}'")
        con.execute(f"SET http_timeout={config.http_timeout_seconds}")
        con.execute(f"SET http_retries={config.http_retries}")
        con.execute(f"SET enable_http_metadata_cache={str(config.http_metadata_cache).lower()}")
        con.execute(f"SET enable_external_file_cache={str(config.external_file_cache).lower()}")

    def require_extension(self, name: str) -> None:
        """Load an optional extension, or explain why the operation cannot run.

        Loading is attempted once per session and the outcome cached, so a
        machine without access to the community repository pays one failed
        install rather than one per call.
        """
        cached = self._optional.get(name)
        if cached is True:
            return
        if cached is False:
            raise CapabilityUnavailableError(
                f"the DuckDB '{name}' extension is not available in this environment, "
                f"so this operation cannot run here"
            )
        repository = COMMUNITY_EXTENSIONS.get(name)
        clause = f" FROM {repository}" if repository else ""
        try:
            self._connection.execute(f"INSTALL {name}{clause}")
            self._connection.execute(f"LOAD {name}")
        except duckdb.Error as exc:
            self._optional[name] = False
            raise CapabilityUnavailableError(
                f"could not load the DuckDB '{name}' extension (INSTALL {name}{clause}): {exc}"
            ) from exc
        self._optional[name] = True

    @contextmanager
    def measure(self) -> Iterator[Measurement]:
        """Account for every byte a block of queries pulls over HTTP.

        Yields a `Measurement` whose queries run on a private cursor. On exit
        its `report` holds the bytes, request count, distinct remote files and
        wall-clock duration for that block alone — correct even when other
        measurements run concurrently.
        """
        cursor = self._connection.cursor()
        self._apply_settings(cursor)
        connection_id = cursor.execute("SELECT current_connection_id()").fetchone()[0]
        # Taken on the measured cursor so every later query on it has a
        # strictly greater id. Queries issued elsewhere keep their own ids and
        # are excluded by the connection filter regardless.
        watermark = cursor.execute("SELECT current_query_id()").fetchone()[0]

        with self._lock:
            self._in_flight += 1

        report = ScanReport()
        started = time.perf_counter()
        try:
            yield Measurement(cursor, report)
        finally:
            report.elapsed_ms = (time.perf_counter() - started) * 1000
            totals = self._connection.execute(
                _HTTP_BYTES_SQL, [connection_id, watermark]
            ).fetchone()
            if totals is not None:
                report.bytes_scanned = int(totals[0])
                report.http_requests = int(totals[1])
                report.remote_files_touched = int(totals[2])
            with self._lock:
                self._in_flight -= 1
                if self._in_flight == 0:
                    # Safe only here: no window is watching the log, so
                    # nothing loses bytes and the table stops growing.
                    self._connection.execute("CALL truncate_duckdb_logs()")
            cursor.close()

    def close(self) -> None:
        """Close the underlying connection. The engine is unusable afterwards."""
        self._connection.close()


_SESSION: Session | None = None
_SESSION_LOCK = threading.Lock()


def get_session(config: SessionConfig | None = None) -> Session:
    """Return the process-wide session, creating it on first use.

    The engine is a singleton because the two things worth keeping — loaded
    extensions and DuckDB's Parquet footer cache — are per-database-instance.
    A per-request connection throws both away and re-reads every footer.

    `config` is honoured only when the session does not exist yet; use
    `reset_session()` to install a different one.
    """
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = Session(config)
    return _SESSION


def reset_session(config: SessionConfig | None = None) -> Session:
    """Discard the current session and start a cold one.

    Used by the benchmark, which needs a session with empty caches to measure
    what a first query really costs over the network.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.close()
        _SESSION = Session(config)
    return _SESSION


def clamp_limit(limit: int) -> int:
    """Keep a caller-supplied row limit inside the session ceiling."""
    return max(1, min(int(limit), MAX_ROW_LIMIT))


def _explain_duckdb_error(exc: duckdb.Error) -> str:
    """Turn a DuckDB failure into something a caller can act on."""
    text = str(exc).strip()
    lowered = text.lower()
    if "http error" in lowered or "404" in lowered:
        return (
            f"the remote dataset could not be read ({text}). The Overture release may have "
            f"expired; re-resolving the release usually fixes it."
        )
    if "referenced column" in lowered or "not found in from clause" in lowered:
        return f"{text} — call the schema operation to list the columns this dataset has."
    return text
