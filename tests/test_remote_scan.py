"""End-to-end checks against the live remote dataset.

Marked `network` because they read Overture's public bucket. They are the
tests that would catch the two failures that actually matter: the release path
expiring, and the pushdown silently stopping.

Run only these with: pytest -m network
Skip them with:      pytest -m "not network"
"""

from __future__ import annotations

import pytest

from geoparquet_mcp.tools import discovery, spatial

pytestmark = pytest.mark.network

# A small slice of central Paris, enough to match without reading much.
PARIS = {"min_lon": 2.33, "min_lat": 48.85, "max_lon": 2.36, "max_lat": 48.87}


def test_describe_source_reads_footers_only() -> None:
    described = discovery.describe_source("overture_places")
    assert described["row_count"] > 50_000_000
    assert described["remote_bytes"] > 1_000_000_000
    # Footers of a 10 GB dataset are tens of megabytes at most.
    assert described["scan"]["bytes_scanned"] < described["remote_bytes"] / 100


def test_bbox_query_returns_features_inside_the_box() -> None:
    result = spatial.bbox_query(**PARIS, limit=25)
    assert result["rows"], "no features returned for central Paris"
    # Overture stores bbox members as 32-bit floats even though the logical
    # type is DOUBLE, so a feature sitting on the edge of the query rectangle
    # reads back a fraction of a metre outside it. The predicate is an
    # intersection test, so that row is a correct match; the tolerance is
    # float32 precision, not slack in the filter.
    tolerance = 1e-4
    for row in result["rows"]:
        assert PARIS["min_lon"] - tolerance <= row["longitude"] <= PARIS["max_lon"] + tolerance
        assert PARIS["min_lat"] - tolerance <= row["latitude"] <= PARIS["max_lat"] + tolerance
    assert result["scan"]["bytes_scanned"] > 0


def test_nearest_is_ordered_and_within_the_radius() -> None:
    result = spatial.nearest(lon=2.3499, lat=48.8530, radius_km=0.5, limit=10)
    distances = [row["distance_km"] for row in result["rows"]]
    assert distances == sorted(distances)
    assert all(distance <= 0.5 for distance in distances)


def test_pushdown_actually_reduces_the_bytes_read() -> None:
    report = spatial.pushdown_report(**PARIS, mode="single_file")
    assert report["matches"] > 0
    assert report["with_pushdown"]["bytes_scanned"] > 0
    assert report["without_pushdown"]["bytes_scanned"] > report["with_pushdown"]["bytes_scanned"], (
        "filter pushdown no longer reduces the bytes read; the project's claim is broken"
    )
    assert report["pushdown_ratio"] > 2
    assert report["download_avoided_ratio"] > 10
