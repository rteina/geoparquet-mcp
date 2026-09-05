"""A tiny GeoParquet corpus, laid out exactly like the remote one.

Why this exists
---------------
Until now the only tests that exercised the engine over real data were the
sixteen marked `network`. They read Overture's public bucket, take about two
minutes, and break whenever a release expires — so CI cannot run them, and
the operations themselves were effectively untested wherever CI looks.

This module builds the same thing locally: a handful of Parquet parts under
`<root>/<release>/theme=<theme>/type=<subtype>/part-NNNNN.parquet`, carrying
Overture's column shapes — a `bbox` STRUCT with per-row-group statistics, a
GEOMETRY column that DuckDB writes GeoParquet metadata for, `names.primary`
and `categories.primary` nested under structs, an `addresses` list of
structs. A `DatasetScope` over it differs from the remote one in exactly one
field: `Source.root`. Every code path below the scope is the same code path.

The data is small and deliberate, so the tests assert exact numbers rather
than "more than zero":

  * 100 places on a 10x10 grid inside the unit square, at 0.05, 0.15 … 0.95
    in both axes. Nothing sits on a tenth or on 0.5, so no assertion depends
    on which side of a boundary a point falls.
  * 6 more places placed to be awkward on purpose: three in a far cluster
    carrying the nulls, two straddling the antimeridian, one near the pole.
  * 5 division polygons: the unit square, and its four quadrants. Every grid
    point is inside the square and exactly 25 are inside each quadrant, which
    is the ground truth the containment join is checked against.

What it deliberately cannot test
--------------------------------
Bytes. A local read makes no HTTP request, so every `scan` block here reports
zero — correctly. What predicate pushdown *saves* is a property of reading
over the network, and it stays measured by the `network` tests and by
`geoparquet_mcp.benchmark`. What is checked here is everything else: that the
operations return the right rows, in the right shape, and refuse the wrong
arguments with a message worth reading.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb

from geoparquet_mcp.engine.sources import Source

# Fixed, so nothing here ever resolves a release over the network.
FIXTURE_RELEASE = "2026-01-01.0"

PLACES = "fixture_places"
DIVISIONS = "fixture_divisions"
ELSEWHERE = "fixture_elsewhere"

# The 10x10 grid: 100 points at 0.05, 0.15 … 0.95 on both axes.
GRID_SIDE = 10
GRID_STEP = 0.1
GRID_ORIGIN = 0.05
GRID_COUNT = GRID_SIDE * GRID_SIDE

# Cycled by the grid index, so the counts are exact: 34, 33, 33.
CATEGORIES = ("cafe", "park", "shop")
CATEGORY_COUNTS = {"cafe": 34, "park": 33, "shop": 33}

# The three awkward-on-purpose clusters, outside the unit square so they never
# perturb a grid assertion.
OUTLIER_LATS = 5.05
OUTLIER_LONS = (5.05, 5.15, 5.25)
OUTLIER_BOX = {"min_lon": 5.0, "min_lat": 5.0, "max_lon": 5.3, "max_lat": 5.1}
ANTIMERIDIAN_LAT = 0.05
POLAR = (0.05, 89.95)

# The unit square the grid lives in, and its four quadrants.
UNIT_SQUARE = {"min_lon": 0.0, "min_lat": 0.0, "max_lon": 1.0, "max_lat": 1.0}
QUADRANT_COUNT = 25

# Every quadrant polygon is a county; the enclosing square is a region.
QUADRANTS = (
    ("Southwest", 0.0, 0.0, 0.5, 0.5),
    ("Southeast", 0.5, 0.0, 1.0, 0.5),
    ("Northwest", 0.0, 0.5, 0.5, 1.0),
    ("Northeast", 0.5, 0.5, 1.0, 1.0),
)

_PLACES_COLUMNS = (
    "id",
    "names.primary AS name",
    "categories.primary AS category",
    "confidence",
    "addresses[1].freeform AS address",
    "bbox.xmin AS longitude",
    "bbox.ymin AS latitude",
)

_DIVISION_COLUMNS = ("id", "names.primary AS name", "subtype", "class", "country", "region")


def _place_row(index: int, identifier: str, lon: float, lat: float) -> dict[str, Any]:
    """One place, with the nested shapes Overture uses."""
    return {
        "id": identifier,
        "name": f"Place {index:02d}",
        "category": CATEGORIES[index % len(CATEGORIES)],
        "confidence": round((index % 10 + 1) / 10, 1),
        "address": f"Street {index}",
        "lon": lon,
        "lat": lat,
    }


def grid_places() -> list[dict[str, Any]]:
    """The 100 regular points, in file order."""
    rows = []
    for index in range(GRID_COUNT):
        column, row = divmod(index, GRID_SIDE)
        rows.append(
            _place_row(
                index,
                f"g{index:03d}",
                # Rounded so the ground truth is the literal a test writes:
                # 0.05 + 0.1 * 1 is 0.15000000000000002 in binary floating point.
                round(GRID_ORIGIN + GRID_STEP * column, 2),
                round(GRID_ORIGIN + GRID_STEP * row, 2),
            )
        )
    return rows


def awkward_places() -> list[dict[str, Any]]:
    """The six points that exist to make an edge case checkable.

    Three carry the nulls, far enough away that a query over the grid never
    sees them; two sit either side of the antimeridian; one is near the pole,
    where a radius search has to stop widening its box.
    """
    return [
        # A missing label, and a missing category and confidence.
        {**_place_row(0, "o0", OUTLIER_LONS[0], OUTLIER_LATS), "name": None},
        {
            **_place_row(1, "o1", OUTLIER_LONS[1], OUTLIER_LATS),
            "category": None,
            "confidence": None,
        },
        _place_row(2, "o2", OUTLIER_LONS[2], OUTLIER_LATS),
        {**_place_row(3, "w0", 179.95, ANTIMERIDIAN_LAT), "name": "Just west of the line"},
        {**_place_row(4, "w1", -179.95, ANTIMERIDIAN_LAT), "name": "Just east of the line"},
        {**_place_row(5, "n0", *POLAR), "name": "Almost polar"},
    ]


def division_rows() -> list[dict[str, Any]]:
    """The enclosing square and its four quadrants."""
    rows = [
        {
            "id": "d0",
            "name": "Alpha",
            "subtype": "region",
            "class": "land",
            **UNIT_SQUARE,
        }
    ]
    for index, (name, min_lon, min_lat, max_lon, max_lat) in enumerate(QUADRANTS, start=1):
        rows.append(
            {
                "id": f"d{index}",
                "name": name,
                "subtype": "county",
                "class": "land",
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            }
        )
    return rows


_PLACE_SELECT = """
SELECT
    id,
    {'primary': name} AS names,
    {'primary': category} AS categories,
    CAST(confidence AS DOUBLE) AS confidence,
    [{'freeform': address, 'locality': 'Gridtown', 'country': 'XX'}] AS addresses,
    {
        'xmin': CAST(lon AS DOUBLE), 'xmax': CAST(lon AS DOUBLE),
        'ymin': CAST(lat AS DOUBLE), 'ymax': CAST(lat AS DOUBLE)
    } AS bbox,
    ST_Point(lon, lat) AS geometry
FROM rows
"""

_DIVISION_SELECT = """
SELECT
    id,
    {'primary': name} AS names,
    subtype,
    class,
    'XX' AS country,
    'XX-A' AS region,
    {
        'xmin': CAST(min_lon AS DOUBLE), 'xmax': CAST(max_lon AS DOUBLE),
        'ymin': CAST(min_lat AS DOUBLE), 'ymax': CAST(max_lat AS DOUBLE)
    } AS bbox,
    ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat) AS geometry
FROM rows
"""


def _write_part(
    connection: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    select: str,
    destination: Path,
) -> None:
    """Write one Parquet part from a list of plain dicts."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0])
    values = ", ".join(
        "(" + ", ".join(_literal(row[column]) for column in columns) + ")" for row in rows
    )
    connection.execute(
        f"CREATE OR REPLACE TEMP VIEW rows AS "
        f"SELECT * FROM (VALUES {values}) AS t({', '.join(columns)})"
    )
    connection.execute(f"COPY ({select}) TO '{destination}' (FORMAT parquet)")


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return repr(value)


def build(root: Path) -> Path:
    """Write the corpus under `root` and return it.

    Idempotent: an existing corpus is overwritten in place, so a stale part
    file cannot survive a change to this module.
    """
    root = Path(root)
    connection = duckdb.connect()
    connection.execute("INSTALL spatial")
    connection.execute("LOAD spatial")
    try:
        places = root / FIXTURE_RELEASE / "theme=places" / "type=place"
        # Two parts, because "how many files did this touch" is part of what
        # the catalogue reports and a single-part corpus could not catch it.
        _write_part(connection, grid_places(), _PLACE_SELECT, places / "part-00000.parquet")
        _write_part(connection, awkward_places(), _PLACE_SELECT, places / "part-00001.parquet")

        divisions = root / FIXTURE_RELEASE / "theme=divisions" / "type=division_area"
        _write_part(connection, division_rows(), _DIVISION_SELECT, divisions / "part-00000.parquet")

        # A third dataset that exists only to be out of scope: the perimeter
        # tests need something registered, real, and readable — so that being
        # refused proves the scope and not a missing file.
        elsewhere = root / FIXTURE_RELEASE / "theme=elsewhere" / "type=elsewhere"
        _write_part(connection, grid_places()[:5], _PLACE_SELECT, elsewhere / "part-00000.parquet")
    finally:
        connection.close()
    return root


def sources_for(root: Path) -> dict[str, Source]:
    """The registry entries for a corpus written at `root`.

    Every field but `root` mirrors the Overture entry it stands in for, so a
    test that passes here is testing the same projections, the same nested
    column references and the same bbox column the remote dataset uses.
    """
    root_path = str(Path(root))
    places = Source(
        name=PLACES,
        title="Fixture — places",
        description="A 10x10 grid of points plus six awkward ones.",
        license="CC0-1.0 (synthetic test data)",
        attribution="© nobody, generated by tests/corpus.py",
        theme="places",
        subtype="place",
        category_column="categories.primary",
        confidence_column="confidence",
        default_columns=_PLACES_COLUMNS,
        approximate_rows=GRID_COUNT + 6,
        approximate_bytes=100_000,
        notes="Synthetic. Two parts.",
        root=root_path,
    )
    return {
        PLACES: places,
        DIVISIONS: Source(
            name=DIVISIONS,
            title="Fixture — division areas",
            description="The unit square and its four quadrants.",
            license="CC0-1.0 (synthetic test data)",
            attribution="© nobody, generated by tests/corpus.py",
            theme="divisions",
            subtype="division_area",
            category_column="subtype",
            polygonal=True,
            default_columns=_DIVISION_COLUMNS,
            approximate_rows=5,
            approximate_bytes=10_000,
            notes="Synthetic. One part.",
            root=root_path,
        ),
        # Same shape as places; a different theme, so a different prefix.
        ELSEWHERE: replace(
            places,
            name=ELSEWHERE,
            title="Fixture — a dataset kept out of scope",
            theme="elsewhere",
            subtype="elsewhere",
            approximate_rows=5,
        ),
    }
