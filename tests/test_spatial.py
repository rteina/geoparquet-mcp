"""Bounding-box construction and the SQL it produces.

These tests are the guard on the project's central claim: the spatial
predicate has to stay expressed as plain comparisons on the bbox struct, or
DuckDB loses the ability to prune row groups and the whole point is gone.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoparquet_mcp.tools.spatial import BoundingBox, _bbox_around

PARIS = BoundingBox(min_lon=2.20, min_lat=48.80, max_lon=2.47, max_lat=48.91)


def test_bbox_rejects_inverted_bounds() -> None:
    with pytest.raises(ValidationError):
        BoundingBox(min_lon=3.0, min_lat=48.0, max_lon=2.0, max_lat=49.0)


def test_bbox_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValidationError):
        BoundingBox(min_lon=-200.0, min_lat=0.0, max_lon=10.0, max_lat=1.0)


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


def test_predicate_honours_a_custom_column_name() -> None:
    assert "envelope.xmin" in PARIS.predicate("envelope")


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
