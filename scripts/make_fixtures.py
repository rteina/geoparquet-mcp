#!/usr/bin/env python3
"""Write the local GeoParquet corpus the unit tests read.

The tests build this themselves, into a temporary directory, once per run —
so nothing here is required to run them. This script exists to make the corpus
inspectable: to open the parts in DuckDB, look at what the generator actually
produced, and check that a change to `tests/corpus.py` did what was intended.

    ./scripts/make_fixtures.py                 # writes ./data/fixtures
    ./scripts/make_fixtures.py --root /tmp/fx  # somewhere else
    ./scripts/make_fixtures.py --describe      # and print what came out

The output is gitignored (`*.parquet`, `data/`): the corpus is derived from
this repository's own code, so committing it would mean carrying a copy that
can silently disagree with the generator.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

import corpus  # noqa: E402  - needs the path above

DEFAULT_ROOT = REPO / "data" / "fixtures"


def describe(root: Path) -> None:
    """Print what was written, read back out of the files themselves."""
    import duckdb

    connection = duckdb.connect()
    connection.execute("INSTALL spatial")
    connection.execute("LOAD spatial")
    try:
        for name, source in corpus.sources_for(root).items():
            target = source.scan_target(corpus.FIXTURE_RELEASE)
            parts, rows, size = connection.execute(
                f"SELECT count(*), sum(num_rows), sum(file_size_bytes) "
                f"FROM parquet_file_metadata('{target}')"
            ).fetchone()
            print(f"\n{name}: {rows} rows in {parts} part(s), {size:,} bytes")
            print(f"  {target}")
            for column, kind, *_ in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{target}')"
            ).fetchall():
                print(f"    {column:<12} {kind}")
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"where to write the corpus (default: {DEFAULT_ROOT.relative_to(REPO)})",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print the row counts, sizes and schema of what was written",
    )
    arguments = parser.parse_args(argv)

    root = corpus.build(arguments.root)
    written = sorted(root.rglob("*.parquet"))
    print(f"wrote {len(written)} Parquet part(s) under {root}")
    for path in written:
        print(f"  {path.relative_to(root)}")
    if arguments.describe:
        describe(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
