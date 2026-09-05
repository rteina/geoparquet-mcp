"""Every engine operation, against the local corpus.

These are the tests CI runs. They are hermetic — a session with no `httpfs`
loaded reads Parquet parts on disk — so they are fast, they do not expire with
an Overture release, and they can assert exact numbers instead of "more than
zero". The corpus is 106 points on a known grid and five polygons whose
containment counts are arithmetic; see `tests/corpus.py`.

What they do not check is bytes. A local read issues no HTTP request, so every
`scan` block here reports zero, which is correct and is asserted once below so
that nobody reads it as a broken measurement. What pushdown *saves* is a
property of reading over a network and stays in `test_remote_scan.py`.
"""

from __future__ import annotations

import pytest

import corpus
from geoparquet_mcp import engine
from geoparquet_mcp.engine.errors import (
    CapabilityUnavailableError,
    InvalidRequestError,
    UnknownColumnError,
    UnknownSourceError,
)

PLACES = corpus.PLACES
DIVISIONS = corpus.DIVISIONS

# The rectangle holding the whole grid, and a corner of it holding four points.
WHOLE_GRID = corpus.UNIT_SQUARE
CORNER = {"min_lon": 0.0, "min_lat": 0.0, "max_lon": 0.2, "max_lat": 0.2}
CORNER_IDS = ["g000", "g001", "g010", "g011"]
# Inside the grid's extent but between its points: a legal box that matches
# nothing, which is a different answer from a box that is itself illegal.
EMPTY_BOX = {"min_lon": 0.96, "min_lat": 0.96, "max_lon": 0.99, "max_lat": 0.99}


def _ids(result: dict) -> list[str]:
    return sorted(feature["id"] for feature in result["geojson"]["features"])


# ---------------------------------------------------------------------------
# Catalogue, schema, extent — the questions answered from footers alone
# ---------------------------------------------------------------------------


def test_the_catalogue_counts_rows_and_parts_exactly(engine_kwargs) -> None:
    listing = engine.list_datasets(exact=True, **engine_kwargs)
    by_name = {entry["name"]: entry for entry in listing["datasets"]}
    assert listing["scope"] == [DIVISIONS, PLACES]
    assert by_name[PLACES]["row_count"] == corpus.GRID_COUNT + 6
    assert by_name[PLACES]["remote_files"] == 2
    assert by_name[DIVISIONS]["row_count"] == 5
    assert by_name[PLACES]["counts_are_exact"] is True


def test_the_catalogue_reads_nothing_when_the_counts_may_be_approximate(engine_kwargs) -> None:
    """`exact=False` is a promise about cost, so it is checked as one."""
    listing = engine.list_datasets(exact=False, **engine_kwargs)
    entry = next(e for e in listing["datasets"] if e["name"] == PLACES)
    assert entry["counts_are_exact"] is False
    assert entry["row_count"] == entry["approximate_rows"]
    assert listing["scan"]["http_requests"] == 0


def test_the_schema_names_the_role_of_every_column_that_has_one(engine_kwargs) -> None:
    described = engine.dataset_schema(PLACES, **engine_kwargs)
    roles = {column["name"]: column["role"] for column in described["columns"]}
    assert roles["geometry"] == "geometry"
    assert roles["bbox"] == "bbox"
    assert roles["names"] == "name"
    assert roles["categories"] == "category"
    assert roles["confidence"] == "confidence"
    assert roles["id"] is None
    assert described["row_count"] == corpus.GRID_COUNT + 6


def test_the_schema_reports_the_geoparquet_metadata_from_the_footer(engine_kwargs) -> None:
    """The `geo` key, which is where the CRS actually lives — not the Arrow schema."""
    described = engine.dataset_schema(PLACES, **engine_kwargs)
    assert described["crs"] == "OGC:CRS84"
    assert described["crs_is_default"] is True
    assert described["encoding"] == "WKB"
    assert described["geometry_types"] == ["Point"]
    assert described["geoparquet_version"]


def test_the_extent_comes_from_statistics_and_covers_the_awkward_points(
    engine_kwargs,
) -> None:
    """The two antimeridian points and the polar one are what set three of the four edges."""
    extent = engine.dataset_extent(PLACES, **engine_kwargs)
    assert extent["from_statistics"] is True
    assert extent["extent"] == {
        "min_lon": -179.95,
        "min_lat": 0.05,
        "max_lon": 179.95,
        "max_lat": 89.95,
    }
    # One row group per part, and the extent is the union of both.
    assert extent["row_groups_examined"] == 2


def test_the_extent_of_the_polygon_dataset_is_the_unit_square(engine_kwargs) -> None:
    assert engine.dataset_extent(DIVISIONS, **engine_kwargs)["extent"] == corpus.UNIT_SQUARE


# ---------------------------------------------------------------------------
# Spatial filtering
# ---------------------------------------------------------------------------


def test_a_rectangle_returns_exactly_the_features_inside_it(engine_kwargs) -> None:
    result = engine.bbox_query(**CORNER, source=PLACES, **engine_kwargs)
    assert _ids(result) == CORNER_IDS
    assert result["geojson"]["type"] == "FeatureCollection"
    assert result["geojson"]["features"][0]["geometry"] == {
        "type": "Point",
        "coordinates": [0.05, 0.05],
    }


def test_the_whole_grid_is_a_hundred_features_and_nothing_else(engine_kwargs) -> None:
    """The six awkward points sit outside the unit square, so they must not appear."""
    result = engine.bbox_query(**WHOLE_GRID, source=PLACES, limit=1000, **engine_kwargs)
    assert result["feature_count"] == corpus.GRID_COUNT
    assert all(feature["id"].startswith("g") for feature in result["geojson"]["features"])


def test_a_box_that_matches_nothing_is_an_empty_answer_not_an_error(engine_kwargs) -> None:
    result = engine.bbox_query(**EMPTY_BOX, source=PLACES, **engine_kwargs)
    assert result["feature_count"] == 0
    assert result["geojson"]["features"] == []
    assert result["truncated"] is False


def test_a_local_read_reports_no_bytes_because_it_makes_no_request(engine_kwargs) -> None:
    """Asserted once, so a zero `scan` block here is documented rather than suspicious.

    The measurement counts HTTP GETs. Reading a file on disk issues none, so
    zero is the right answer and the byte accounting itself is exercised
    against the remote dataset in `test_session.py` and `test_remote_scan.py`.
    """
    scan = engine.bbox_query(**CORNER, source=PLACES, **engine_kwargs)["scan"]
    assert scan == {
        "bytes_scanned": 0,
        "megabytes_scanned": 0.0,
        "http_requests": 0,
        "remote_files_touched": 0,
        "elapsed_ms": scan["elapsed_ms"],
    }
    assert scan["elapsed_ms"] > 0


def test_skipping_the_geometry_column_still_places_the_feature(engine_kwargs) -> None:
    """The synthesised point is the bbox corner, which for a point dataset is the point."""
    exact = engine.bbox_query(**CORNER, source=PLACES, **engine_kwargs)
    synthesised = engine.bbox_query(
        **CORNER, source=PLACES, include_geometry=False, **engine_kwargs
    )
    assert synthesised["geometry_is_exact"] is False
    assert _ids(synthesised) == _ids(exact)
    assert (
        synthesised["geojson"]["features"][0]["geometry"]
        == (exact["geojson"]["features"][0]["geometry"])
    )


def test_a_wkt_geometry_is_tested_exactly_not_just_by_its_envelope(engine_kwargs) -> None:
    """A triangle whose envelope holds four grid points but which contains one.

    The envelope is what prunes; `ST_Intersects` is what decides. If the exact
    re-test were dropped this would return the envelope's four.
    """
    triangle = "POLYGON ((0 0, 0.19 0, 0 0.19, 0 0))"
    result = engine.spatial_filter(source=PLACES, wkt=triangle, **engine_kwargs)
    assert _ids(result) == ["g000"]
    # The pruning rectangle is reported, and it is the triangle's envelope.
    assert result["bbox"] == {"min_lon": 0.0, "min_lat": 0.0, "max_lon": 0.19, "max_lat": 0.19}


def test_a_wkt_square_and_the_equivalent_rectangle_agree(engine_kwargs) -> None:
    square = "POLYGON ((0 0, 0.2 0, 0.2 0.2, 0 0.2, 0 0))"
    assert _ids(engine.spatial_filter(source=PLACES, wkt=square, **engine_kwargs)) == CORNER_IDS


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"category": "cafe"}, corpus.CATEGORY_COUNTS["cafe"]),
        ({"category": "park"}, corpus.CATEGORY_COUNTS["park"]),
        ({"category": "no_such_category"}, 0),
        # "Place 00" through "Place 09": ten of them, matched case-insensitively.
        ({"name_contains": "place 0"}, 10),
        ({"name_contains": "PLACE 0"}, 10),
        # Confidence cycles 0.1 … 1.0, ten grid points each.
        ({"min_confidence": 0.9}, 20),
        ({"min_confidence": 1.0}, 10),
        ({"category": "cafe", "min_confidence": 0.9}, 7),
    ],
)
def test_attribute_filters_narrow_the_answer(engine_kwargs, filters, expected) -> None:
    result = engine.bbox_query(**WHOLE_GRID, source=PLACES, limit=1000, **filters, **engine_kwargs)
    assert result["feature_count"] == expected


def test_a_named_projection_replaces_the_default_one(engine_kwargs) -> None:
    result = engine.bbox_query(
        **CORNER, source=PLACES, columns=["id", "confidence"], **engine_kwargs
    )
    assert set(result["geojson"]["features"][0]["properties"]) == {"id", "confidence"}


def test_the_row_limit_is_clamped_and_the_answer_says_it_was_truncated(
    engine_kwargs,
) -> None:
    capped = engine.bbox_query(**WHOLE_GRID, source=PLACES, limit=10_000, **engine_kwargs)
    assert capped["limit"] == engine.MAX_ROW_LIMIT

    truncated = engine.bbox_query(**WHOLE_GRID, source=PLACES, limit=5, **engine_kwargs)
    assert truncated["feature_count"] == 5
    assert truncated["truncated"] is True


# ---------------------------------------------------------------------------
# Refusals — an argument the engine cannot act on
# ---------------------------------------------------------------------------


def test_a_zero_row_limit_is_refused_rather_than_silently_raised(engine_kwargs) -> None:
    """`clamp_limit` would turn 0 into 1; validation rejects it first, which is honest."""
    with pytest.raises(InvalidRequestError, match="greater than or equal to 1"):
        engine.bbox_query(**WHOLE_GRID, source=PLACES, limit=0, **engine_kwargs)


def test_an_inverted_rectangle_is_refused_before_any_read(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError, match="min_lon must be smaller"):
        engine.bbox_query(
            min_lon=0.5, min_lat=0.0, max_lon=0.1, max_lat=1.0, source=PLACES, **engine_kwargs
        )


def test_a_rectangle_crossing_the_antimeridian_is_refused_and_says_why(
    engine_kwargs,
) -> None:
    """Not applicable here, and saying so is the answer.

    A box from 179 to -179 is the shape a caller reaches for to cross the
    date line. This engine's rectangle is a single interval per axis, so that
    box is not a wide box — it is an inverted one, and accepting it would
    silently select the other 358 degrees of the world. It is refused, and
    the corpus keeps a point either side of the line so that the two halves,
    queried separately, are demonstrably reachable.
    """
    with pytest.raises(InvalidRequestError, match="min_lon must be smaller"):
        engine.bbox_query(
            min_lon=179.0,
            min_lat=0.0,
            max_lon=-179.0,
            max_lat=1.0,
            source=PLACES,
            **engine_kwargs,
        )

    east = engine.bbox_query(
        min_lon=179.0, min_lat=0.0, max_lon=180.0, max_lat=1.0, source=PLACES, **engine_kwargs
    )
    west = engine.bbox_query(
        min_lon=-180.0, min_lat=0.0, max_lon=-179.0, max_lat=1.0, source=PLACES, **engine_kwargs
    )
    assert _ids(east) == ["w0"]
    assert _ids(west) == ["w1"]


def test_a_half_specified_rectangle_is_refused_with_the_missing_corners_named(
    engine_kwargs,
) -> None:
    with pytest.raises(InvalidRequestError, match="all four"):
        engine.spatial_filter(source=PLACES, min_lon=0.0, min_lat=0.0, **engine_kwargs)


def test_giving_both_a_rectangle_and_a_geometry_is_refused(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError, match="exactly one"):
        engine.spatial_filter(source=PLACES, wkt="POINT (0 0)", **WHOLE_GRID, **engine_kwargs)


def test_giving_neither_is_refused(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError, match="all four"):
        engine.spatial_filter(source=PLACES, **engine_kwargs)


def test_unparseable_wkt_is_explained_with_an_example(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError) as caught:
        engine.spatial_filter(source=PLACES, wkt="not a geometry", **engine_kwargs)
    assert "POLYGON" in str(caught.value), "the message must show the shape it wanted"


def test_a_filter_a_dataset_cannot_support_says_so_by_name(engine_kwargs) -> None:
    """Divisions carry no confidence, so the filter is refused rather than ignored."""
    with pytest.raises(InvalidRequestError, match="no confidence column"):
        engine.bbox_query(**WHOLE_GRID, source=DIVISIONS, min_confidence=0.5, **engine_kwargs)


def test_a_dataset_outside_the_scope_lists_the_ones_inside_it(engine_kwargs) -> None:
    with pytest.raises(UnknownSourceError) as caught:
        engine.bbox_query(**WHOLE_GRID, source=corpus.ELSEWHERE, **engine_kwargs)
    assert PLACES in str(caught.value)


# ---------------------------------------------------------------------------
# Nearest
# ---------------------------------------------------------------------------


def test_nearest_orders_by_distance_and_stops_at_the_radius(engine_kwargs) -> None:
    """The grid spacing is 0.1 degrees, about 11.1 km, so the ranking is arithmetic."""
    result = engine.nearest(lon=0.05, lat=0.05, radius_km=12.0, source=PLACES, **engine_kwargs)
    assert [row["id"] for row in result["rows"]][0] == "g000"
    assert {row["id"] for row in result["rows"]} == {"g000", "g001", "g010"}
    distances = [row["distance_km"] for row in result["rows"]]
    assert distances == sorted(distances)
    assert distances[0] == 0.0
    assert all(distance <= 12.0 for distance in distances)


def test_a_radius_that_reaches_nothing_returns_an_empty_ranking(engine_kwargs) -> None:
    result = engine.nearest(lon=100.0, lat=10.0, radius_km=1.0, source=PLACES, **engine_kwargs)
    assert result["row_count"] == 0
    assert result["rows"] == []


def test_a_search_near_the_pole_produces_a_legal_box(engine_kwargs) -> None:
    """The longitude half-width blows up as the cosine goes to zero; it is clamped.

    Without the clamp the pruning rectangle would fail its own validation and
    the query would never run.
    """
    result = engine.nearest(lon=0.05, lat=89.95, radius_km=100.0, source=PLACES, **engine_kwargs)
    box = result["search_bbox"]
    assert -180.0 <= box["min_lon"] < box["max_lon"] <= 180.0
    assert box["max_lat"] <= 90.0
    assert [row["id"] for row in result["rows"]] == ["n0"]


@pytest.mark.parametrize("radius_km", [0.0, -1.0, 501.0])
def test_an_impossible_radius_is_refused(engine_kwargs, radius_km) -> None:
    with pytest.raises(InvalidRequestError, match="radius_km"):
        engine.nearest(lon=0.05, lat=0.05, radius_km=radius_km, source=PLACES, **engine_kwargs)


# ---------------------------------------------------------------------------
# Column statistics
# ---------------------------------------------------------------------------


def test_a_numeric_column_returns_a_summary_and_a_histogram_that_add_up(
    engine_kwargs,
) -> None:
    stats = engine.column_statistics(
        column="confidence", source=PLACES, **WHOLE_GRID, histogram_buckets=5, **engine_kwargs
    )
    assert stats["kind"] == "numeric"
    assert stats["row_count"] == corpus.GRID_COUNT
    assert stats["distinct_values"] == 10
    assert stats["summary"]["minimum"] == 0.1
    assert stats["summary"]["maximum"] == 1.0
    assert stats["summary"]["p25"] <= stats["summary"]["median"] <= stats["summary"]["p75"]
    assert len(stats["distribution"]) == 5
    assert sum(bucket["count"] for bucket in stats["distribution"]) == stats["non_null_count"]
    assert sum(bucket["share"] for bucket in stats["distribution"]) == pytest.approx(1.0)


def test_a_categorical_column_returns_the_top_values_by_share(engine_kwargs) -> None:
    stats = engine.column_statistics(
        column="categories.primary", source=PLACES, **WHOLE_GRID, top_k=2, **engine_kwargs
    )
    assert stats["kind"] == "categorical"
    assert stats["distinct_values"] == len(corpus.CATEGORIES)
    assert [entry["value"] for entry in stats["distribution"]] == ["cafe", "park"]
    assert stats["distribution"][0]["count"] == corpus.CATEGORY_COUNTS["cafe"]
    assert stats["distribution"][0]["share"] == pytest.approx(0.34)
    counts = [entry["count"] for entry in stats["distribution"]]
    assert counts == sorted(counts, reverse=True)


def test_nulls_are_counted_rather_than_dropped(engine_kwargs) -> None:
    """The outlier cluster is three rows, one of which has no confidence."""
    stats = engine.column_statistics(
        column="confidence", source=PLACES, **corpus.OUTLIER_BOX, **engine_kwargs
    )
    assert stats["row_count"] == 3
    assert stats["non_null_count"] == 2
    assert stats["null_count"] == 1


def test_an_unknown_column_lists_the_real_ones_and_explains_nesting(engine_kwargs) -> None:
    with pytest.raises(UnknownColumnError) as caught:
        engine.column_statistics(column="not_a_column", source=PLACES, **engine_kwargs)
    message = str(caught.value)
    assert "confidence" in message, "the message must list the columns that do exist"
    assert "categories.primary" in message, "and say how a nested field is addressed"


def test_a_partial_bounding_box_is_refused_rather_than_half_applied(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError, match="all four"):
        engine.column_statistics(
            column="confidence", source=PLACES, min_lon=0.0, max_lon=1.0, **engine_kwargs
        )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_counting_by_category_returns_the_corpus_counts(engine_kwargs) -> None:
    result = engine.attribute_aggregate(
        "categories.primary", source=PLACES, **WHOLE_GRID, **engine_kwargs
    )
    assert {row["group_value"]: row["value"] for row in result["groups"]} == (
        corpus.CATEGORY_COUNTS
    )
    assert result["rows_aggregated"] == corpus.GRID_COUNT
    assert result["truncated"] is False


def test_an_aggregate_over_a_measure_column_runs_remotely(engine_kwargs) -> None:
    result = engine.attribute_aggregate(
        "categories.primary",
        source=PLACES,
        aggregate="avg",
        measure="confidence",
        **WHOLE_GRID,
        **engine_kwargs,
    )
    averages = {row["group_value"]: row["value"] for row in result["groups"]}
    assert set(averages) == set(corpus.CATEGORIES)
    assert all(0.1 <= value <= 1.0 for value in averages.values())


def test_a_non_numeric_measure_is_refused_before_any_read(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError, match="needs a numeric measure"):
        engine.attribute_aggregate(
            "categories.primary", source=PLACES, aggregate="avg", measure="id", **engine_kwargs
        )


def test_count_takes_no_measure_and_the_others_require_one(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError, match="takes no `measure`"):
        engine.attribute_aggregate(
            "categories.primary", source=PLACES, measure="confidence", **engine_kwargs
        )
    with pytest.raises(InvalidRequestError, match="needs a `measure`"):
        engine.attribute_aggregate(
            "categories.primary", source=PLACES, aggregate="sum", **engine_kwargs
        )


def test_grouping_by_a_column_that_does_not_exist_names_the_ones_that_do(
    engine_kwargs,
) -> None:
    with pytest.raises(UnknownColumnError, match="confidence"):
        engine.attribute_aggregate("not_a_column", source=PLACES, **engine_kwargs)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_returns_file_order_and_says_so(engine_kwargs) -> None:
    result = engine.preview_rows(source=PLACES, limit=3, **engine_kwargs)
    assert [row["id"] for row in result["rows"]] == ["g000", "g001", "g002"]
    assert "not geographic" in result["ordering"]
    assert result["columns_returned"] == list(result["rows"][0])


@pytest.mark.parametrize("limit", [0, 101])
def test_preview_refuses_a_limit_outside_its_range(engine_kwargs, limit) -> None:
    with pytest.raises(InvalidRequestError, match="limit"):
        engine.preview_rows(source=PLACES, limit=limit, **engine_kwargs)


# ---------------------------------------------------------------------------
# H3
# ---------------------------------------------------------------------------


def test_h3_bins_every_feature_and_a_finer_resolution_only_splits_cells(
    engine_kwargs,
) -> None:
    try:
        coarse = engine.h3_aggregate(**WHOLE_GRID, resolution=4, source=PLACES, **engine_kwargs)
    except CapabilityUnavailableError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"h3 extension unavailable: {exc}")
    fine = engine.h3_aggregate(
        **WHOLE_GRID, resolution=8, source=PLACES, limit=1000, **engine_kwargs
    )
    assert coarse["features_binned"] == corpus.GRID_COUNT
    assert fine["features_binned"] == corpus.GRID_COUNT
    assert fine["cell_count"] > coarse["cell_count"]
    for cell in coarse["cells"]:
        assert len(cell["h3_cell"]) == 15, "cell ids must be the canonical hex form"
    # Checked at resolution 8, where a cell is under a kilometre across, so a
    # centre has to land near the rectangle. A resolution-4 cell spans degrees
    # and its centre legitimately falls outside a rectangle this small.
    for cell in fine["cells"]:
        assert -0.01 <= cell["centre_lon"] <= 1.01
        assert -0.01 <= cell["centre_lat"] <= 1.01
    counts = [cell["feature_count"] for cell in coarse["cells"]]
    assert counts == sorted(counts, reverse=True)


def test_h3_can_omit_the_cell_centre(engine_kwargs) -> None:
    try:
        result = engine.h3_aggregate(
            **WHOLE_GRID, resolution=4, source=PLACES, include_cell_centre=False, **engine_kwargs
        )
    except CapabilityUnavailableError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"h3 extension unavailable: {exc}")
    assert set(result["cells"][0]) == {"h3_cell", "feature_count"}


@pytest.mark.parametrize("resolution", [-1, 16])
def test_an_h3_resolution_outside_the_scale_is_refused(engine_kwargs, resolution) -> None:
    with pytest.raises(InvalidRequestError, match="resolution"):
        engine.h3_aggregate(**WHOLE_GRID, resolution=resolution, source=PLACES, **engine_kwargs)


# ---------------------------------------------------------------------------
# Point in polygon — the one operation that reads two datasets
# ---------------------------------------------------------------------------


def test_containment_counts_match_the_corpus_arithmetic(engine_kwargs) -> None:
    """Every grid point is in the square; exactly a quarter is in each quadrant."""
    result = engine.point_in_polygon(
        **WHOLE_GRID,
        point_source=PLACES,
        polygon_source=DIVISIONS,
        **engine_kwargs,
    )
    counts = {row["polygon_name"]: row["feature_count"] for row in result["polygons"]}
    assert counts["Alpha"] == corpus.GRID_COUNT
    for name, *_ in corpus.QUADRANTS:
        assert counts[name] == corpus.QUADRANT_COUNT
    assert sum(row["feature_count"] for row in result["polygons"][1:]) == corpus.GRID_COUNT


def test_a_polygon_subtype_narrows_the_containing_side(engine_kwargs) -> None:
    result = engine.point_in_polygon(
        **WHOLE_GRID,
        point_source=PLACES,
        polygon_source=DIVISIONS,
        polygon_subtype="county",
        **engine_kwargs,
    )
    assert result["polygon_count"] == len(corpus.QUADRANTS)
    assert "Alpha" not in {row["polygon_name"] for row in result["polygons"]}


def test_a_point_dataset_cannot_be_the_containing_side(engine_kwargs) -> None:
    with pytest.raises(InvalidRequestError) as caught:
        engine.point_in_polygon(
            **WHOLE_GRID, point_source=PLACES, polygon_source=PLACES, **engine_kwargs
        )
    message = str(caught.value)
    assert "does not hold polygons" in message
    assert DIVISIONS in message, "the message must name a source that would work"


def test_a_containment_join_cannot_reach_outside_the_scope(engine_kwargs) -> None:
    with pytest.raises(UnknownSourceError):
        engine.point_in_polygon(
            **WHOLE_GRID,
            point_source=corpus.ELSEWHERE,
            polygon_source=DIVISIONS,
            **engine_kwargs,
        )


# ---------------------------------------------------------------------------
# Ad-hoc SQL
# ---------------------------------------------------------------------------


def test_a_dataset_in_scope_is_queryable_by_its_own_name(engine_kwargs) -> None:
    result = engine.run_sql(
        f"SELECT categories.primary AS c, count(*) AS n FROM {PLACES} "
        f"WHERE bbox.xmin <= 1 AND bbox.xmax >= 0 AND bbox.ymin <= 1 AND bbox.ymax >= 0 "
        f"GROUP BY 1 ORDER BY 1",
        **engine_kwargs,
    )
    assert {row["c"]: row["n"] for row in result["rows"]} == corpus.CATEGORY_COUNTS
    assert result["tables_read"] == [PLACES]
    assert result["tables_available"] == [DIVISIONS, PLACES]


def test_a_join_across_two_datasets_in_scope_is_allowed(engine_kwargs) -> None:
    result = engine.run_sql(
        f"WITH counties AS (SELECT * FROM {DIVISIONS} WHERE subtype = 'county') "
        f"SELECT count(*) AS n FROM counties",
        **engine_kwargs,
    )
    assert result["rows"] == [{"n": len(corpus.QUADRANTS)}]
    assert result["tables_read"] == [DIVISIONS]


def test_the_outer_row_limit_is_enforced_whatever_the_query_asks(engine_kwargs) -> None:
    result = engine.run_sql(f"SELECT id FROM {PLACES} LIMIT 100", max_rows=4, **engine_kwargs)
    assert result["row_count"] == 4
    assert result["truncated"] is True
    assert result["executed_sql"].endswith("LIMIT 4")


def test_the_byte_budget_is_reported_as_a_verdict_not_a_brake(engine_kwargs) -> None:
    result = engine.run_sql(f"SELECT count(*) AS n FROM {PLACES}", max_bytes=0, **engine_kwargs)
    assert result["rows"] == [{"n": corpus.GRID_COUNT + 6}]
    # A local read moves no bytes at all, so even a zero budget is not exceeded.
    assert result["byte_budget_exceeded"] is False


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("", "empty"),
        ("DROP TABLE fixture_places", "only SELECT is allowed"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("SELECT * FROM fixture_elsewhere", "unknown table"),
        ("SELECT * FROM read_parquet('/etc/passwd')", "table functions are not available"),
        ("SELECT * FROM read_csv('/etc/passwd')", "table functions are not available"),
    ],
)
def test_sql_the_perimeter_refuses(engine_kwargs, sql, expected) -> None:
    with pytest.raises(InvalidRequestError, match=expected):
        engine.run_sql(sql, **engine_kwargs)
