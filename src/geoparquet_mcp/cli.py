"""Command-line entry point: the demo, and the server launcher.

`geoparquet-mcp demo` is the single command a fresh clone runs. It exists to
make the project's claim checkable in under a minute, without an MCP client.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from geoparquet_mcp import __version__, sources
from geoparquet_mcp.tools import discovery, spatial

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


def _rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("─" * 72)


def run_demo(source: str = sources.DEFAULT_SOURCE, as_json: bool = False) -> int:
    """Query a multi-gigabyte remote dataset four ways and report the bytes read."""
    collected: dict[str, Any] = {}

    if not as_json:
        print(
            "\033[1mgeoparquet-mcp demo\033[0m — spatial analysis on a remote file, no import step"
        )

    _rule("1. The dataset, described from its footers") if not as_json else None
    described = discovery.describe_source(source)
    collected["describe_source"] = described
    if not as_json:
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

    _rule(f"2. What is in {DEMO_PLACE}?") if not as_json else None
    aggregate = spatial.bbox_aggregate(**DEMO_BBOX, source=source, limit=8)
    collected["bbox_aggregate"] = aggregate
    if not as_json:
        for row in aggregate["groups"]:
            print(f"  {row['feature_count']:>8,}  {row['group_value']}")
        scan = aggregate["scan"]
        print(f"read: {_mb(scan['bytes_scanned'])} in {scan['elapsed_ms']:,.0f} ms")

    _rule("3. Bakeries within 400 m of Notre-Dame") if not as_json else None
    near = spatial.nearest(
        lon=2.3499,
        lat=48.8530,
        radius_km=0.4,
        source=source,
        category="bakery" if source == "overture_places" else None,
        limit=5,
    )
    collected["nearest"] = near
    if not as_json:
        for row in near["rows"]:
            print(f"  {row['distance_km'] * 1000:>6,.0f} m  {row.get('name') or '(unnamed)'}")
        if not near["rows"]:
            print("  (no match in this release)")
        print(f"read: {_mb(near['scan']['bytes_scanned'])} in {near['scan']['elapsed_ms']:,.0f} ms")

    _rule("4. The point of all this: bytes moved") if not as_json else None
    proof = spatial.pushdown_report(**DEMO_BBOX, source=source, mode="single_file")
    collected["pushdown_report"] = proof
    if not as_json:
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
        print(f"attribution: {aggregate['attribution']} — {described['license']}")

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
    demo.add_argument("--source", default=sources.DEFAULT_SOURCE, help="Registered source name.")
    demo.add_argument("--json", action="store_true", help="Emit raw JSON instead of a report.")

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
    if args.command == "sources":
        json.dump(discovery.list_sources(), sys.stdout, indent=2, default=str)
        print()
        return 0
    if args.command == "serve":
        from geoparquet_mcp.server import build_server

        build_server().run(transport=args.transport)
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
