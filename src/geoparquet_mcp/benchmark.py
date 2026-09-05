"""The measuring bench: every engine operation, timed and weighed.

This is not a capability an agent should have. It is the evidence the README
quotes, kept in the repository so the numbers can be re-derived instead of
trusted, and kept out of `tools/` because "run the same query twice with an
optimiser disabled" is a benchmark, not a question anyone asks a map.

Two things are measured here.

`run_benchmark()` runs each operation five times and reports the median
duration, the median bytes pulled over the network and the rows returned. Each
of those five runs starts from a **cold session**: DuckDB caches Parquet
footers and decoded data pages in memory, and a warm session answers a repeated
query without touching the network at all — 0 bytes, which is a true number
that measures nothing. Cold runs are what a first question against a dataset
actually costs. A warm median is reported alongside, because the gap between
the two is what the singleton session buys.

`pushdown_report()` is the A/B that carries the project's whole argument: the
same aggregate, against the same remote file, with and without DuckDB's filter
pushdown. Each side runs on its own cold session so neither warms the other.

A note on a number that changed
-------------------------------
An earlier version of this A/B ran the pushed query on the same connection
that had just scanned the whole dataset to find which part file held the most
matches. That scan left the matching bbox pages of the chosen part in DuckDB's
page cache, so the pushed query did not have to fetch them and reported
3,295,188 bytes against 125,511,244 — a ratio of 38.1×. The number was real
and reproducible, but it was measuring a warm cache as much as a cold
pushdown.

Measured properly — each side on its own cold session, the shared Parquet
footer accounted separately because no optimiser can avoid it — the same query
reads 6,850,570 bytes with pushdown against 128,806,432 without: 18.8×. The
unpushed side is unchanged; it was always cold. The pushed side doubled once it
had to pay for its own pages. 18.8× is the honest figure, and it is still the
whole argument.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from geoparquet_mcp import engine
from geoparquet_mcp.engine.sources import DatasetScope

# Paris, roughly the périphérique: small enough that the pruning is dramatic,
# dense enough that the answer is interesting.
PARIS = {"min_lon": 2.20, "min_lat": 48.80, "max_lon": 2.47, "max_lat": 48.91}
# A tighter box, for the operations that would otherwise dominate the table.
PARIS_CENTRE = {"min_lon": 2.33, "min_lat": 48.85, "max_lon": 2.36, "max_lat": 48.87}

DEFAULT_RUNS = 5

# The benchmark works inside a deliberately narrow scope: the two datasets it
# actually reads. `overture_buildings` is registered but has 513 part files,
# and reading 513 footers five times would measure Overture's file layout
# rather than this engine.
BENCHMARK_SOURCES = ("overture_places", "overture_divisions")


def benchmark_scope() -> DatasetScope:
    """The dataset perimeter the benchmark runs inside."""
    return DatasetScope.restricted_to(BENCHMARK_SOURCES)


# ---------------------------------------------------------------------------
# The A/B that carries the argument
# ---------------------------------------------------------------------------


def pushdown_report(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    source: str = engine.DEFAULT_SOURCE,
    scope: DatasetScope | None = None,
    mode: Literal["single_file", "whole_dataset"] = "single_file",
) -> dict[str, Any]:
    """Measure what predicate pushdown saves, by running the query both ways.

    The same aggregate runs twice against the same remote data: once normally,
    once with DuckDB's filter pushdown optimiser disabled so that every row
    group is fetched. The difference is the claim this project makes, produced
    on demand rather than quoted from a README.

    `single_file` restricts both runs to the one Parquet part holding the most
    matches, so the unpushed run stays affordable; `whole_dataset` measures the
    pushed run against every part and compares it to the dataset's total remote
    size instead.

    Each phase runs on a cold session. Sharing one would let the pushed run
    warm DuckDB's page cache for the unpushed run and understate the gap.
    """
    scope = scope or benchmark_scope()
    definition = scope.get(source)
    target = scope.target(source)
    box = engine.BoundingBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)
    where = box.predicate(definition.bbox_column)

    # Phase 1 — footprint, and which single part holds the most matches.
    session = engine.reset_session()
    with session.measure() as metadata:
        footprint = metadata.one(
            f"""
            SELECT count(*) AS remote_files, sum(file_size_bytes) AS remote_bytes
            FROM parquet_file_metadata('{target}')
            """
        )
        if mode == "single_file":
            busiest = metadata.records(
                f"""
                SELECT filename, count(*) AS matches
                FROM read_parquet('{target}', filename = true)
                WHERE {where}
                GROUP BY 1 ORDER BY matches DESC LIMIT 1
                """
            )
            if not busiest:
                raise ValueError("no features match this bounding box; widen it and retry")
            # The one place a path rather than a name reaches a read. The scope
            # checks it against its own prefixes before it is used.
            scan_target = scope.assert_within(busiest[0]["filename"])
            file_bytes = metadata.one(
                f"SELECT file_size_bytes FROM parquet_file_metadata('{scan_target}')"
            )["file_size_bytes"]
        else:
            scan_target = target
            file_bytes = footprint["remote_bytes"]

    # `max(length(...))` forces a real column read, so the comparison is not
    # answered out of Parquet metadata alone.
    measured_sql = (
        f"SELECT count(*) AS matches, max(length(names.primary)) AS longest_name "
        f"FROM read_parquet('{scan_target}') WHERE {where}"
    )

    # Phase 2 — with pushdown. Phase 3 — the same query, optimiser off.
    pushed = _run_phase(scan_target, measured_sql, disable_pushdown=False)
    unpushed = (
        _run_phase(scan_target, measured_sql, disable_pushdown=True)
        if mode == "single_file"
        else None
    )

    pushed_bytes = pushed["data"]["bytes_scanned"]
    result: dict[str, Any] = {
        "source": definition.name,
        "release": scope.release,
        "mode": mode,
        "bbox": box.as_dict(),
        "scan_target": scan_target,
        "matches": pushed["matches"],
        "sql": measured_sql,
        "remote_files": footprint["remote_files"],
        "dataset_remote_bytes": footprint["remote_bytes"],
        "baseline_bytes_if_downloaded": file_bytes,
        "with_pushdown": pushed["data"],
        "without_pushdown": unpushed["data"] if unpushed else None,
        "with_pushdown_cold": pushed["cold"],
        "without_pushdown_cold": unpushed["cold"] if unpushed else None,
        "footer_bytes": pushed["footer"]["bytes_scanned"],
        "metadata_scan": metadata.report.as_dict(),
    }
    if pushed_bytes:
        result["download_avoided_ratio"] = round(file_bytes / pushed_bytes, 1)
        if unpushed:
            result["pushdown_ratio"] = round(unpushed["data"]["bytes_scanned"] / pushed_bytes, 1)
            result["cold_ratio"] = round(
                unpushed["cold"]["bytes_scanned"] / pushed["cold"]["bytes_scanned"], 1
            )
    return result


def _run_phase(target: str, measured_sql: str, disable_pushdown: bool) -> dict[str, Any]:
    """Run the comparison query once on a cold session, footer cost separated.

    A cold DuckDB session must read the Parquet footer before it can read any
    data, and that footer is tens of megabytes on an Overture part file. Left
    inside the measurement it lands identically on both sides of the A/B and
    drags the ratio towards 1 — it dilutes the very effect being measured,
    because a footer read is not something pushdown can avoid.

    So the footer read is measured separately and the query is measured after
    it. `data` is what pushdown actually decides; `cold` is footer + data,
    which is what a genuine first query costs. Both are reported, because they
    answer different questions.
    """
    session = engine.reset_session()

    with session.measure() as footer:
        footer.one(f"SELECT count(*) AS parts FROM parquet_file_metadata('{target}')")

    with session.measure() as data:
        if disable_pushdown:
            with data.option("disabled_optimizers", "filter_pushdown"):
                row = data.one(measured_sql)
        else:
            row = data.one(measured_sql)

    footer_report = footer.report.as_dict()
    data_report = data.report.as_dict()
    return {
        "matches": row["matches"],
        "footer": footer_report,
        "data": data_report,
        "cold": {
            "bytes_scanned": footer_report["bytes_scanned"] + data_report["bytes_scanned"],
            "megabytes_scanned": round(
                (footer_report["bytes_scanned"] + data_report["bytes_scanned"]) / 1_000_000, 3
            ),
            "http_requests": footer_report["http_requests"] + data_report["http_requests"],
            "remote_files_touched": max(
                footer_report["remote_files_touched"], data_report["remote_files_touched"]
            ),
            "elapsed_ms": round(footer_report["elapsed_ms"] + data_report["elapsed_ms"], 1),
        },
    }


# ---------------------------------------------------------------------------
# The per-operation table
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """One operation to measure, and how to count what it returned."""

    label: str
    call: Callable[[], dict[str, Any]]
    rows: Callable[[dict[str, Any]], int]
    note: str = ""
    # The datasets this case reads. Their footers are read, and measured,
    # before the operation runs, so the operation's own bytes are data pages
    # rather than metadata every case would pay identically.
    datasets: tuple[str, ...] = ("overture_places",)


@dataclass
class Result:
    """The measurements for one case.

    Three states are worth distinguishing, and conflating them is how a
    benchmark ends up reporting a true number that measures nothing:

    * `footer_*` — reading the Parquet footers on a cold session. A fixed
      toll, tens of megabytes on Overture, that every first query pays and no
      query can avoid.
    * `query_*` — the operation itself with those footers in cache. This is
      what the operation costs, and the only column that tells one operation
      apart from another.
    * `warm_ms` — the same call again on the same session, with DuckDB's page
      cache warm too. Bytes are omitted here because they are 0: a true number
      that says nothing about the work.
    """

    label: str
    note: str
    footer_ms: list[float] = field(default_factory=list)
    footer_bytes: list[int] = field(default_factory=list)
    query_ms: list[float] = field(default_factory=list)
    query_bytes: list[int] = field(default_factory=list)
    warm_ms: list[float] = field(default_factory=list)
    rows: int = 0
    error: str | None = None

    @property
    def median_cold_ms(self) -> float:
        return statistics.median(self.footer_ms) + statistics.median(self.query_ms)

    @property
    def median_cold_bytes(self) -> int:
        return int(statistics.median(self.footer_bytes)) + int(statistics.median(self.query_bytes))

    def as_dict(self) -> dict[str, Any]:
        if self.error:
            return {"operation": self.label, "error": self.error}
        return {
            "operation": self.label,
            "note": self.note,
            "median_query_ms": round(statistics.median(self.query_ms), 1),
            "median_query_bytes": int(statistics.median(self.query_bytes)),
            "median_footer_ms": round(statistics.median(self.footer_ms), 1),
            "median_footer_bytes": int(statistics.median(self.footer_bytes)),
            "median_cold_ms": round(self.median_cold_ms, 1),
            "median_cold_bytes": self.median_cold_bytes,
            "median_warm_ms": round(statistics.median(self.warm_ms), 1) if self.warm_ms else None,
            "rows_returned": self.rows,
            "runs": len(self.query_ms),
        }


def build_cases(scope: DatasetScope) -> list[Case]:
    """The operations the benchmark measures, one case each."""
    return [
        Case(
            "list_datasets (exact)",
            lambda: engine.list_datasets(scope=scope, exact=True),
            lambda r: len(r["datasets"]),
            "row counts and sizes from Parquet footers",
            datasets=("overture_places", "overture_divisions"),
        ),
        Case(
            "dataset_schema",
            lambda: engine.dataset_schema("overture_places", scope=scope),
            lambda r: len(r["columns"]),
            "columns and types, footers only",
        ),
        Case(
            "dataset_extent",
            lambda: engine.dataset_extent("overture_places", scope=scope),
            lambda r: 1,
            "whole-dataset bbox from row-group statistics",
        ),
        Case(
            "bbox_query (GeoJSON)",
            lambda: engine.bbox_query(**PARIS_CENTRE, scope=scope, limit=50, include_geometry=True),
            lambda r: r["feature_count"],
            "50 features with true geometry",
        ),
        Case(
            "bbox_query (no geometry)",
            lambda: engine.bbox_query(
                **PARIS_CENTRE, scope=scope, limit=50, include_geometry=False
            ),
            lambda r: r["feature_count"],
            "same query, geometry column not read",
        ),
        Case(
            "nearest",
            lambda: engine.nearest(
                lon=2.3499,
                lat=48.8530,
                radius_km=0.4,
                category="bakery",
                scope=scope,
                limit=20,
            ),
            lambda r: r["row_count"],
            "bakeries within 400 m of Notre-Dame",
        ),
        Case(
            "column_statistics (categorical)",
            lambda: engine.column_statistics(
                column="categories.primary", **PARIS_CENTRE, scope=scope, top_k=25
            ),
            lambda r: len(r["distribution"]),
            "top categories in central Paris",
        ),
        Case(
            "column_statistics (numeric)",
            lambda: engine.column_statistics(
                column="confidence", **PARIS_CENTRE, scope=scope, histogram_buckets=10
            ),
            lambda r: len(r["distribution"]),
            "confidence summary and histogram",
        ),
        Case(
            "h3_aggregate (res 9)",
            lambda: engine.h3_aggregate(**PARIS, resolution=9, scope=scope, limit=200),
            lambda r: r["cell_count"],
            "features binned into H3 cells",
        ),
        Case(
            "point_in_polygon",
            lambda: engine.point_in_polygon(
                **PARIS_CENTRE, polygon_subtype="county", scope=scope, limit=50
            ),
            lambda r: r["polygon_count"],
            "places per administrative area",
            datasets=("overture_places", "overture_divisions"),
        ),
    ]


def _read_footers(session, scope: DatasetScope, datasets: tuple[str, ...]) -> Any:
    """Read the Parquet footers of the given datasets, and report what it cost."""
    with session.measure() as measurement:
        for name in datasets:
            measurement.one(
                f"SELECT count(*) AS parts FROM parquet_file_metadata('{scope.target(name)}')"
            )
    return measurement.report


def measure_case(case: Case, runs: int, scope: DatasetScope) -> Result:
    """Run one case `runs` times cold, then `runs` times warm.

    Each cold run starts from a fresh DuckDB session, reads the footers of the
    datasets the case touches, and only then runs the operation — so the
    operation's reported bytes are the data pages its predicates actually
    needed, not the metadata toll every case pays alike.
    """
    result = Result(label=case.label, note=case.note)
    try:
        for _ in range(runs):
            session = engine.reset_session()
            footer = _read_footers(session, scope, case.datasets)
            result.footer_ms.append(footer.elapsed_ms)
            result.footer_bytes.append(footer.bytes_scanned)

            started = time.perf_counter()
            payload = case.call()
            result.query_ms.append((time.perf_counter() - started) * 1000)
            result.query_bytes.append(payload["scan"]["bytes_scanned"])
            result.rows = case.rows(payload)
        for _ in range(runs):
            started = time.perf_counter()
            case.call()
            result.warm_ms.append((time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - a failed case must not stop the bench
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def run_benchmark(runs: int = DEFAULT_RUNS, scope: DatasetScope | None = None) -> list[Result]:
    """Measure every operation and return the results in table order."""
    scope = scope or benchmark_scope()
    return [measure_case(case, runs, scope) for case in build_cases(scope)]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _mb(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1_000_000:,.2f}"


def print_table(results: list[Result], runs: int) -> None:
    header = (
        f"{'operation':<32} {'query ms':>9} {'warm ms':>8} {'query bytes':>13} "
        f"{'query MB':>9} {'+footer MB':>11} {'cold ms':>8} {'rows':>6}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        if result.error:
            print(f"{result.label:<32} {'FAILED':>9}  {result.error}")
            continue
        row = result.as_dict()
        print(
            f"{row['operation']:<32} {row['median_query_ms']:>9,.0f} "
            f"{row['median_warm_ms']:>8,.0f} {row['median_query_bytes']:>13,} "
            f"{_mb(row['median_query_bytes']):>9} {_mb(row['median_footer_bytes']):>11} "
            f"{row['median_cold_ms']:>8,.0f} {row['rows_returned']:>6,}"
        )
    print("-" * len(header))
    print(f"medians over {runs} runs. Each run starts from a fresh DuckDB session.")
    print(
        "query   = the operation, with the Parquet footers already read. This is what the\n"
        "          operation costs, and the only column that tells two of them apart."
    )
    print(
        "+footer = the footers themselves, read cold before the operation. A fixed toll no\n"
        "          query can avoid, paid once per session — which is why the session is a\n"
        "          singleton with the HTTP metadata cache on."
    )
    print("cold    = footer + query: what a genuine first call costs.")
    print(
        "warm    = the same call again on the same session. Bytes are omitted because they\n"
        "          are 0: DuckDB answers from its page cache, which measures nothing."
    )


def print_pushdown(report: dict[str, Any]) -> None:
    without = report["without_pushdown"]
    with_ = report["with_pushdown"]
    print(f"scan target                  {report['scan_target']}")
    print(f"matching features            {report['matches']:,}")
    print(
        f"whole dataset, if downloaded {report['dataset_remote_bytes']:>14,} bytes "
        f"({report['remote_files']} files)"
    )
    print(f"one part file, if downloaded {report['baseline_bytes_if_downloaded']:>14,} bytes")
    print(f"that part's Parquet footer   {report['footer_bytes']:>14,} bytes (read by both runs)")
    print(
        f"same query, pushdown OFF     {without['bytes_scanned']:>14,} bytes "
        f"in {without['elapsed_ms']:,.0f} ms"
    )
    print(
        f"same query, pushdown ON      {with_['bytes_scanned']:>14,} bytes "
        f"in {with_['elapsed_ms']:,.0f} ms"
    )
    print()
    ratio = report.get("pushdown_ratio")
    if ratio is None:
        print("  pushdown ratio unavailable: the pushed run read nothing measurable.")
    elif ratio < 1.5:
        print(
            f"  {ratio}× — pushdown is NOT working on this query. The bbox predicate is "
            f"not reaching the Parquet reader; check that it is expressed as plain "
            f"comparisons on the bbox struct members and that the dataset carries "
            f"row-group statistics for them."
        )
    else:
        print(f"  {ratio}× fewer bytes than the same query without pushdown")
    print(f"  {report['download_avoided_ratio']}× fewer bytes than downloading that one file")
    total = report["dataset_remote_bytes"] / report["with_pushdown"]["bytes_scanned"]
    print(f"  {total:,.0f}× fewer bytes than downloading the dataset")
    cold_ratio = report.get("cold_ratio")
    if cold_ratio is not None:
        cold_on = report["with_pushdown_cold"]["bytes_scanned"]
        cold_off = report["without_pushdown_cold"]["bytes_scanned"]
        print()
        print(
            f"  Counting the Parquet footer that both runs must read first, a genuinely cold\n"
            f"  query is {cold_on:,} bytes with pushdown against {cold_off:,} without: "
            f"{cold_ratio}×.\n"
            f"  The footer is a fixed toll pushdown cannot remove, so it dilutes the ratio;\n"
            f"  the {report['pushdown_ratio']}× above is what pushdown itself decides."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="geoparquet-mcp-benchmark",
        description="Time and weigh every engine operation against the remote dataset.",
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of a table.")
    parser.add_argument(
        "--only",
        choices=("all", "operations", "pushdown"),
        default="all",
        help="Which half of the bench to run.",
    )
    args = parser.parse_args(argv)

    scope = benchmark_scope()
    collected: dict[str, Any] = {"release": scope.release, "scope": scope.names}

    if args.only in ("all", "operations"):
        if not args.json:
            print(f"\nOPERATIONS — release {scope.release}, scope {', '.join(scope.names)}\n")
        results = run_benchmark(runs=args.runs, scope=scope)
        collected["operations"] = [result.as_dict() for result in results]
        if not args.json:
            print_table(results, args.runs)

    if args.only in ("all", "pushdown"):
        if not args.json:
            print("\n\nPUSHDOWN A/B — same query, same file, optimiser on and off\n")
        report = pushdown_report(**PARIS, scope=scope, mode="single_file")
        collected["pushdown"] = report
        if not args.json:
            print_pushdown(report)

    if args.json:
        json.dump(collected, sys.stdout, indent=2, default=str)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
