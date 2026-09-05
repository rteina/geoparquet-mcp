"""The connection singleton and, above all, the correctness of measurement.

Byte accounting is the project's evidence, so the way it can silently break
deserves a test. DuckDB's HTTP log is one table for the whole database
instance. The natural implementation — truncate the log, run the query, sum
the log — is correct for exactly one query at a time and quietly wrong the
moment two overlap: whichever query truncates last erases the other's
evidence, and whichever reads last claims the other's bytes.

`Session.measure()` attributes rather than truncates. These tests are what
makes that claim checkable.
"""

from __future__ import annotations

import threading

import pytest

from geoparquet_mcp.engine import sources
from geoparquet_mcp.engine.session import Session, SessionConfig, clamp_limit, get_session

# A small slice of central Paris: enough matches to move real bytes.
PARIS = "bbox.xmin <= 2.36 AND bbox.xmax >= 2.33 AND bbox.ymin <= 48.87 AND bbox.ymax >= 48.85"


def _places_target() -> str:
    """The Overture places glob at whatever release currently resolves."""
    return sources.SOURCES["overture_places"].scan_target(sources.resolve_release())


@pytest.fixture
def session() -> Session:
    own = Session(SessionConfig(threads=4))
    yield own
    own.close()


def test_the_session_is_a_singleton() -> None:
    assert get_session() is get_session()


def test_the_metadata_cache_is_on() -> None:
    """Without it every query re-reads the same Parquet footers over HTTP."""
    own = Session()
    try:
        value = own._connection.execute(
            "SELECT current_setting('enable_http_metadata_cache')"
        ).fetchone()
        assert value[0] is True
    finally:
        own.close()


def test_a_measurement_with_no_remote_read_reports_zero(session: Session) -> None:
    with session.measure() as measurement:
        assert measurement.one("SELECT 42 AS answer")["answer"] == 42
    report = measurement.report
    assert report.bytes_scanned == 0
    assert report.http_requests == 0
    assert report.elapsed_ms > 0


def test_row_limits_are_clamped() -> None:
    assert clamp_limit(0) == 1
    assert clamp_limit(10_000) == 1000
    assert clamp_limit(50) == 50


@pytest.mark.network
def test_two_measurements_do_not_steal_each_others_bytes(session: Session) -> None:
    """The regression this design exists to prevent.

    A local-only measurement runs concurrently with one that reads a remote
    dataset. With a truncate-and-sum implementation the local one would report
    the remote one's bytes (or wipe them). With per-connection attribution it
    must report exactly zero, and the remote one must keep all of its own.
    """
    target = _places_target()
    reports: dict[str, object] = {}
    started = threading.Barrier(2)

    def remote() -> None:
        with session.measure() as measurement:
            started.wait(timeout=30)
            measurement.one(f"SELECT count(*) AS n FROM read_parquet('{target}') WHERE {PARIS}")
        reports["remote"] = measurement.report

    def local() -> None:
        with session.measure() as measurement:
            started.wait(timeout=30)
            for _ in range(200):
                measurement.one("SELECT 1 AS one")
        reports["local"] = measurement.report

    threads = [threading.Thread(target=remote), threading.Thread(target=local)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert set(reports) == {"remote", "local"}, "a measurement thread did not finish"
    assert reports["local"].bytes_scanned == 0, (
        "a purely local measurement was charged for another connection's HTTP traffic"
    )
    assert reports["local"].http_requests == 0
    assert reports["remote"].bytes_scanned > 0, "the remote measurement lost its own bytes"


@pytest.mark.network
def test_a_second_measurement_does_not_inherit_the_first(session: Session) -> None:
    """Sequential windows must not double-count: the watermark is per window."""
    target = _places_target()
    with session.measure() as first:
        first.one(f"SELECT count(*) AS n FROM parquet_file_metadata('{target}')")
    assert first.report.bytes_scanned >= 0
    assert first.report.http_requests > 0

    with session.measure() as second:
        second.one("SELECT 1 AS one")
    assert second.report.bytes_scanned == 0
    assert second.report.http_requests == 0
