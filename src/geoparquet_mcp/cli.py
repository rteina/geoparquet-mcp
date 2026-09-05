"""Command-line entry point: the demo, the benchmark, and the server launcher.

`geoparquet-mcp demo` is the single command a fresh clone runs. It exists to
make the project's claim checkable in under a minute, without an MCP client.
It talks to the engine directly, which is the point: nothing about these
answers needs a protocol.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from geoparquet_mcp import __version__, benchmark, engine

# Paris, roughly the périphérique. Small enough that the pruning is dramatic,
# dense enough that the answer is interesting.
DEMO_BBOX = {"min_lon": 2.20, "min_lat": 48.80, "max_lon": 2.47, "max_lat": 48.91}
DEMO_PLACE = "Paris, France"


def _mb(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value / 1_000_000:,.1f} MB"


def _gb(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value / 1_000_000_000:,.2f} GB"


def _read_line(scan: dict[str, Any]) -> str:
    """How much this step pulled over the network, and why it might be nothing.

    Later steps often read zero bytes: the session is a singleton, so DuckDB
    still has the footers and pages the earlier steps fetched. Printing a bare
    "0.0 MB" reads like a broken counter, so say what it means.
    """
    if scan["bytes_scanned"] == 0:
        return f"read: nothing — already in the session cache ({scan['elapsed_ms']:,.0f} ms)"
    return f"read: {_mb(scan['bytes_scanned'])} in {scan['elapsed_ms']:,.0f} ms"


def _rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("─" * 72)


def run_demo(source: str = engine.DEFAULT_SOURCE, as_json: bool = False) -> int:
    """Query a multi-gigabyte remote dataset five ways and report the bytes read."""
    collected: dict[str, Any] = {}
    show = not as_json

    # The perimeter, resolved once here and passed to every call below. The
    # demo is the engine used directly, without a protocol: the same object
    # the application injects into handlers, built by the caller instead.
    scope = engine.DatasetScope.default()

    if show:
        print(
            "\033[1mgeoparquet-mcp demo\033[0m — spatial analysis on a remote file, no import step"
        )

    if show:
        _rule("1. The dataset, described from its footers")
    described = engine.dataset_schema(source=source, scope=scope)
    collected["describe_source"] = described
    if show:
        print(f"source        {described['title']}")
        print(f"licence       {described['license']}")
        print(f"release       {described['release']}")
        print(f"location      {described['https_prefix']}")
        print(
            f"size          {described['row_count']:,} rows, "
            f"{described['remote_files']} files, {_gb(described['remote_bytes'])}, "
            f"{described['row_groups']:,} row groups"
        )
        print(f"read to learn all of that: {_mb(described['scan']['bytes_scanned'])}")

    if show:
        _rule(f"2. What is in {DEMO_PLACE}?")
    category_column = described["category_column"] or "class"
    aggregate = engine.column_statistics(
        column=category_column, **DEMO_BBOX, source=source, top_k=8, scope=scope
    )
    collected["column_statistics"] = aggregate
    if show:
        for row in aggregate["distribution"]:
            print(f"  {row['count']:>8,}  {row['value'] or '(uncategorised)'}")
        print(
            f"{aggregate['row_count']:,} features, {aggregate['distinct_values']:,} distinct "
            f"{category_column} values"
        )
        print(_read_line(aggregate["scan"]))

    if show:
        _rule("3. Bakeries within 400 m of Notre-Dame")
    near = engine.nearest(
        lon=2.3499,
        lat=48.8530,
        radius_km=0.4,
        source=source,
        category="bakery" if source == "overture_places" else None,
        limit=5,
        scope=scope,
    )
    collected["nearest"] = near
    if show:
        for row in near["rows"]:
            print(f"  {row['distance_km'] * 1000:>6,.0f} m  {row.get('name') or '(unnamed)'}")
        if not near["rows"]:
            print("  (no match in this release)")
        print(_read_line(near["scan"]))

    if show:
        _rule("4. Where is it densest? H3 cells, computed remotely")
    density = engine.h3_aggregate(**DEMO_BBOX, resolution=8, source=source, limit=5, scope=scope)
    collected["h3_aggregate"] = density
    if show:
        for row in density["cells"]:
            print(
                f"  {row['feature_count']:>8,}  {row['h3_cell']}  "
                f"({row['centre_lat']:.4f}, {row['centre_lon']:.4f})"
            )
        print(f"{density['cell_count']} cells at resolution {density['resolution']}")
        print(_read_line(density["scan"]))

    if show:
        _rule("5. The point of all this: bytes moved")
    proof = benchmark.pushdown_report(**DEMO_BBOX, source=source, scope=scope, mode="single_file")
    collected["pushdown_report"] = proof
    if show:
        without = proof["without_pushdown"]
        print(f"matching features            {proof['matches']:,}")
        print(
            f"whole dataset, if downloaded {_gb(proof['dataset_remote_bytes'])} "
            f"({proof['remote_files']} files)"
        )
        print(f"one part file, if downloaded {_mb(proof['baseline_bytes_if_downloaded'])}")
        print(
            f"same query, pushdown OFF     {_mb(without['bytes_scanned'])} "
            f"in {without['elapsed_ms']:,.0f} ms"
        )
        print(
            f"same query, pushdown ON      {_mb(proof['with_pushdown']['bytes_scanned'])} "
            f"in {proof['with_pushdown']['elapsed_ms']:,.0f} ms"
        )
        print()
        print(
            f"  \033[1m{proof['pushdown_ratio']}× fewer bytes"
            " than the same query without pushdown\033[0m"
        )
        print(
            f"  \033[1m{proof['download_avoided_ratio']}× fewer bytes"
            " than downloading that one file\033[0m"
        )
        total_ratio = proof["dataset_remote_bytes"] / proof["with_pushdown"]["bytes_scanned"]
        print(f"  \033[1m{total_ratio:,.0f}× fewer bytes than downloading the dataset\033[0m")
        print()
        print(f"attribution: {described['attribution']} — {described['license']}")

    if as_json:
        json.dump(collected, sys.stdout, indent=2, default=str)
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="geoparquet-mcp",
        description="Spatial analysis over remote GeoParquet files, for MCP clients.",
    )
    parser.add_argument("--version", action="version", version=f"geoparquet-mcp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Run the end-to-end demo against the remote dataset.")
    demo.add_argument("--source", default=engine.DEFAULT_SOURCE, help="Registered source name.")
    demo.add_argument("--json", action="store_true", help="Emit raw JSON instead of a report.")

    bench = sub.add_parser("benchmark", help="Time and weigh every engine operation.")
    bench.add_argument("--runs", type=int, default=benchmark.DEFAULT_RUNS)
    bench.add_argument("--json", action="store_true")
    bench.add_argument("--only", choices=("all", "operations", "pushdown"), default="all")

    serve = sub.add_parser("serve", help="Run the MCP server.")
    serve.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
    )

    catalog = sub.add_parser("sources", help="Print the source catalogue as JSON.")
    catalog.set_defaults(_noop=True)

    args = parser.parse_args(argv)

    if args.command == "demo":
        return run_demo(source=args.source, as_json=args.json)
    if args.command == "benchmark":
        bench_argv = ["--runs", str(args.runs), "--only", args.only]
        if args.json:
            bench_argv.append("--json")
        return benchmark.main(bench_argv)
    if args.command == "sources":
        json.dump(engine.list_datasets()["datasets"], sys.stdout, indent=2, default=str)
        print()
        return 0
    if args.command == "serve":
        # Delegated, not reimplemented. Serving over stdio is two steps —
        # resolve the perimeter from the environment and install it, then run
        # the server — and this command used to do only the second. It started,
        # announced its eight tools, and failed every call that followed on a
        # perimeter nobody had installed. `server.main` is the one place those
        # two steps live.
        from geoparquet_mcp.server import main as serve_over_transport

        serve_over_transport(["--transport", args.transport])
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
