"""Input validation and the SQL the engine builds, without touching the network.

These tests are the guard on the project's central claim: the spatial
predicate has to stay expressed as plain comparisons on the bbox struct, or
DuckDB loses the ability to prune row groups and the whole point is gone.
The rest guard the other contract — that a bad argument comes back as a typed
error a caller can act on, not as a DuckDB stack trace.
"""

from __future__ import annotations

import pytest

from geoparquet_mcp.engine.errors import InvalidRequestError
from geoparquet_mcp.engine.operations import (
    BboxQueryRequest,
    BoundingBox,
    ColumnStatisticsRequest,
    H3AggregateRequest,
    _bbox_around,
    _validated,
)

PARIS = BoundingBox(min_lon=2.20, min_lat=48.80, max_lon=2.47, max_lat=48.91)


# ---------------------------------------------------------------------------
# The predicate that makes pruning possible
# ---------------------------------------------------------------------------


def test_predicate_compares_each_bbox_member_separately() -> None:
    sql = PARIS.predicate()
    # One comparison per struct member is what lets Parquet statistics prune.
    for member in ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax"):
        assert sql.count(member) == 1
    assert "ST_" not in sql, "a geometry function here would defeat row-group pruning"


def test_predicate_is_an_intersection_not_a_containment() -> None:
    sql = PARIS.predicate()
    assert "bbox.xmin <= 2.47" in sql
    assert "bbox.xmax >= 2.2" in sql


def test_predicate_honours_a_custom_column_name_and_alias() -> None:
    assert "envelope.xmin" in PARIS.predicate("envelope")
    assert "poly.bbox.xmin" in PARIS.predicate("bbox", alias="poly")


def test_radius_becomes_a_box_that_contains_the_circle() -> None:
    box = _bbox_around(lon=2.3499, lat=48.8530, radius_km=1.0)
    # ~1 km is ~0.009 degrees of latitude everywhere.
    assert box.max_lat - 48.8530 == pytest.approx(0.00899, abs=1e-4)
    # Longitude degrees are shorter at this latitude, so the box is wider.
    assert (box.max_lon - 2.3499) > (box.max_lat - 48.8530)


def test_radius_box_stays_inside_valid_coordinates_near_the_pole() -> None:
    box = _bbox_around(lon=0.0, lat=89.999, radius_km=50.0)
    assert -180.0 <= box.min_lon < box.max_lon <= 180.0
    assert -90.0 <= box.min_lat < box.max_lat <= 90.0


# ---------------------------------------------------------------------------
# Typed errors with usable messages
# ---------------------------------------------------------------------------


def test_bbox_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="min_lon must be smaller"):
        BoundingBox(min_lon=3.0, min_lat=48.0, max_lon=2.0, max_lat=49.0)


def test_bbox_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValueError):
        BoundingBox(min_lon=-200.0, min_lat=0.0, max_lon=10.0, max_lat=1.0)


def test_validation_failure_names_the_field_and_the_problem() -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        _validated(
            H3AggregateRequest,
            source="overture_places",
            bbox=PARIS,
            resolution=42,
        )
    message = str(excinfo.value)
    assert "resolution" in message
    assert "15" in message, "the message must say what the valid range is"


@pytest.mark.parametrize("resolution", [-1, 16, 100])
def test_h3_resolution_out_of_bounds_is_refused(resolution: int) -> None:
    with pytest.raises(InvalidRequestError):
        _validated(H3AggregateRequest, source="overture_places", bbox=PARIS, resolution=resolution)


@pytest.mark.parametrize("resolution", [0, 8, 15])
def test_h3_resolution_inside_the_range_is_accepted(resolution: int) -> None:
    request = _validated(
        H3AggregateRequest, source="overture_places", bbox=PARIS, resolution=resolution
    )
    assert request.resolution == resolution


# ---------------------------------------------------------------------------
# Column references reach SQL as text, so they are constrained
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "confidence; DROP TABLE x",
        "1 UNION SELECT * FROM read_parquet('s3://elsewhere/*.parquet')",
        "*",
        "names.primary AS x",
        "(SELECT 1)",
        "",
    ],
)
def test_a_column_reference_that_is_not_an_identifier_is_refused(column: str) -> None:
    with pytest.raises(InvalidRequestError):
        _validated(ColumnStatisticsRequest, source="overture_places", column=column)


@pytest.mark.parametrize("column", ["confidence", "categories.primary", "addresses"])
def test_a_plain_dotted_identifier_is_accepted(column: str) -> None:
    request = _validated(ColumnStatisticsRequest, source="overture_places", column=column)
    assert request.column == column


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        ({"min_lon": 3.0, "min_lat": 48.0, "max_lon": 2.0, "max_lat": 49.0}, "min_lon"),
        ({"min_lon": -200.0, "min_lat": 0.0, "max_lon": 10.0, "max_lat": 1.0}, "-180"),
        ({"min_lon": 2.0, "min_lat": 48.0, "max_lon": 3.0, "max_lat": 48.0}, "min_lat"),
    ],
)
def test_an_invalid_bbox_raises_a_typed_engine_error(bbox: dict, expected: str) -> None:
    """A bad rectangle must not escape as a raw pydantic ValidationError.

    An LLM reads these messages to decide what to send next, and pydantic's
    own rendering buries the field under a type tag and a documentation URL.
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        _validated(BboxQueryRequest, source="overture_places", bbox=bbox)
    message = str(excinfo.value)
    # Field-level failures are located as "bbox.min_lon"; the whole-model
    # ordering check is located as "bbox" and names the field in its text.
    assert "bbox" in message, "the message must point at the bounding box"
    assert expected in message, "the message must say what was wrong with it"


def test_a_projection_list_is_constrained_the_same_way() -> None:
    with pytest.raises(InvalidRequestError):
        _validated(
            BboxQueryRequest,
            source="overture_places",
            bbox=PARIS,
            columns=["id", "confidence); DROP TABLE t --"],
        )
