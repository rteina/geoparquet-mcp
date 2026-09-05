"""End-to-end checks against the live remote dataset.

Marked `network` because they read Overture's public bucket. They are the
tests that would catch the failures that actually matter: the release path
expiring, the pushdown silently stopping, and an operation returning something
that is not the shape it promises.

Run only these with: pytest -m network
Skip them with:      pytest -m "not network"
"""

from __future__ import annotations

import pytest

from geoparquet_mcp import benchmark, engine
from geoparquet_mcp.engine.errors import (
    CapabilityUnavailableError,
    InvalidRequestError,
    UnknownColumnError,
)

pytestmark = pytest.mark.network

# A small slice of central Paris, enough to match without reading much.
PARIS = {"min_lon": 2.33, "min_lat": 48.85, "max_lon": 2.36, "max_lat": 48.87}


@pytest.fixture(scope="module")
def scope() -> engine.DatasetScope:
    return benchmark.benchmark_scope()


def test_catalogue_reports_size_for_every_dataset_in_scope(scope) -> None:
    listing = engine.list_datasets(scope=scope, exact=True)
    assert set(listing["scope"]) == {"overture_places", "overture_divisions"}
    for dataset in listing["datasets"]:
        assert dataset["row_count"] > 0
        assert dataset["remote_bytes"] > 0
        assert dataset["counts_are_exact"] is True


def test_schema_reads_footers_only(scope) -> None:
    described = engine.dataset_schema("overture_places", scope=scope)
    assert described["row_count"] > 50_000_000
    assert described["remote_bytes"] > 1_000_000_000
    # Footers of a 10 GB dataset are tens of megabytes at most.
    assert described["scan"]["bytes_scanned"] < described["remote_bytes"] / 100
    roles = {column["role"] for column in described["columns"]}
    assert {"geometry", "bbox"} <= roles


def test_extent_comes_from_statistics_and_covers_the_world(scope) -> None:
    extent = engine.dataset_extent("overture_places", scope=scope)
    assert extent["from_statistics"] is True
    box = extent["extent"]
    assert box["min_lon"] < -170 and box["max_lon"] > 170
    assert box["min_lat"] < -80 and box["max_lat"] > 80
    assert extent["row_groups_examined"] > 1000


def test_bbox_query_returns_geojson_inside_the_box(scope) -> None:
    result = engine.bbox_query(**PARIS, scope=scope, limit=25)
    collection = result["geojson"]
    assert collection["type"] == "FeatureCollection"
    assert collection["features"], "no features returned for central Paris"
    # Overture stores bbox members as 32-bit floats even though the logical
    # type is DOUBLE, so a feature sitting on the edge of the query rectangle
    # reads back a fraction of a metre outside it. The predicate is an
    # intersection test, so that row is a correct match; the tolerance is
    # float32 precision, not slack in the filter.
    tolerance = 1e-4
    for feature in collection["features"]:
        assert feature["type"] == "Feature"
        lon, lat = feature["geometry"]["coordinates"]
        assert PARIS["min_lon"] - tolerance <= lon <= PARIS["max_lon"] + tolerance
        assert PARIS["min_lat"] - tolerance <= lat <= PARIS["max_lat"] + tolerance
        assert "name" in feature["properties"]
    assert result["scan"]["bytes_scanned"] > 0


def _data_bytes(scope, **overrides) -> tuple[dict, int]:
    """Run one bbox query on a cold session, with the footer read paid separately.

    A cold session must fetch Parquet footers before it can fetch any data, and
    on Overture places that is about twenty megabytes against a ten-gigabyte
    file. Counting it inside the measurement puts the same large number on both
    sides of a comparison and buries the difference being measured — which for
    fifty rows is a few hundred kilobytes of geometry. So the footer is read
    first, in its own window, and only the query that follows is compared.
    This is the same separation `benchmark._run_phase` makes, for the same reason.
    """
    session = engine.reset_session()
    target = scope.target("overture_places")
    with session.measure() as footer:
        footer.one(f"SELECT count(*) AS parts FROM parquet_file_metadata('{target}')")
    result = engine.bbox_query(**PARIS, scope=scope, session=session, limit=50, **overrides)
    return result, result["scan"]["bytes_scanned"]


def test_skipping_the_geometry_column_reads_fewer_bytes(scope) -> None:
    """The projection is worth as much as the filter on a wide dataset."""
    _, with_geometry = _data_bytes(scope, include_geometry=True)
    without_result, without = _data_bytes(scope, include_geometry=False)
    assert without_result["geometry_is_exact"] is False
    assert with_geometry > 0, "the measured window read nothing at all"
    assert without < with_geometry, (
        f"dropping the widest column in the file did not reduce the data read "
        f"({without} vs {with_geometry} bytes)"
    )


def test_nearest_is_ordered_and_within_the_radius(scope) -> None:
    result = engine.nearest(lon=2.3499, lat=48.8530, radius_km=0.5, scope=scope, limit=10)
    distances = [row["distance_km"] for row in result["rows"]]
    assert distances == sorted(distances)
    assert all(distance <= 0.5 for distance in distances)


def test_categorical_statistics_describe_the_distribution(scope) -> None:
    stats = engine.column_statistics(column="categories.primary", **PARIS, scope=scope, top_k=10)
    assert stats["kind"] == "categorical"
    assert stats["row_count"] > 1000
    assert stats["distinct_values"] > 50
    assert len(stats["distribution"]) == 10
    counts = [entry["count"] for entry in stats["distribution"]]
    assert counts == sorted(counts, reverse=True)
    assert 0 < stats["distribution"][0]["share"] <= 1


def test_numeric_statistics_return_a_summary_and_a_histogram(scope) -> None:
    stats = engine.column_statistics(column="confidence", **PARIS, scope=scope, histogram_buckets=8)
    assert stats["kind"] == "numeric"
    summary = stats["summary"]
    assert 0.0 <= summary["minimum"] <= summary["median"] <= summary["maximum"] <= 1.0
    assert summary["p25"] <= summary["median"] <= summary["p75"]
    assert len(stats["distribution"]) <= 8
    assert sum(entry["count"] for entry in stats["distribution"]) == stats["non_null_count"]


def test_an_unknown_column_says_which_columns_exist(scope) -> None:
    with pytest.raises(UnknownColumnError) as excinfo:
        engine.column_statistics(column="not_a_column", **PARIS, scope=scope)
    message = str(excinfo.value)
    assert "confidence" in message, "the error must list the real columns"
    assert "categories.primary" in message, "the error must explain nested access"


def test_h3_aggregation_bins_features_into_cells(scope) -> None:
    try:
        result = engine.h3_aggregate(**PARIS, resolution=9, scope=scope, limit=100)
    except CapabilityUnavailableError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"h3 extension unavailable: {exc}")
    assert result["cell_count"] > 0
    counts = [cell["feature_count"] for cell in result["cells"]]
    assert counts == sorted(counts, reverse=True)
    for cell in result["cells"]:
        # Canonical H3 hexadecimal cell id.
        assert len(cell["h3_cell"]) == 15
        assert PARIS["min_lat"] - 0.01 <= cell["centre_lat"] <= PARIS["max_lat"] + 0.01
    # A finer resolution can only split cells, never merge them.
    coarse = engine.h3_aggregate(**PARIS, resolution=7, scope=scope, limit=1000)
    fine = engine.h3_aggregate(**PARIS, resolution=10, scope=scope, limit=1000)
    assert fine["cell_count"] > coarse["cell_count"]


def test_point_in_polygon_counts_places_inside_an_administrative_area(scope) -> None:
    result = engine.point_in_polygon(**PARIS, polygon_subtype="county", scope=scope, limit=10)
    assert result["polygons"], "central Paris should fall inside at least one county"
    names = {row["polygon_name"] for row in result["polygons"]}
    assert "Paris" in names
    # Every point in the box is inside Paris, so the containment count must
    # match the plain bbox count.
    total = engine.column_statistics(column="confidence", **PARIS, scope=scope)["row_count"]
    paris_row = next(row for row in result["polygons"] if row["polygon_name"] == "Paris")
    assert paris_row["feature_count"] == total


def test_a_dataset_outside_the_scope_cannot_be_read(scope) -> None:
    with pytest.raises(InvalidRequestError):
        engine.bbox_query(**PARIS, source="overture_buildings", scope=scope, limit=1)


def test_pushdown_actually_reduces_the_bytes_read(scope) -> None:
    report = benchmark.pushdown_report(**PARIS, scope=scope, mode="single_file")
    assert report["matches"] > 0
    assert report["with_pushdown"]["bytes_scanned"] > 0
    assert report["without_pushdown"]["bytes_scanned"] > report["with_pushdown"]["bytes_scanned"], (
        "filter pushdown no longer reduces the bytes read; the project's claim is broken"
    )
    assert report["pushdown_ratio"] > 2
    assert report["download_avoided_ratio"] > 10
