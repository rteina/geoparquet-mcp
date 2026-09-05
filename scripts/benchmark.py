#!/usr/bin/env python3
"""Run the measuring bench and print the numbers the README quotes.

    ./scripts/benchmark.py                 # operations table, then the pushdown A/B
    ./scripts/benchmark.py --runs 3
    ./scripts/benchmark.py --only pushdown
    ./scripts/benchmark.py --json > benchmark.json

A thin runner, on purpose: the bench itself lives in
`geoparquet_mcp.benchmark` so that the demo can reuse its pushdown A/B and so
that it can be imported and tested like anything else. This file only gets the
package importable — from the source tree, and from the project virtualenv if
this interpreter has no DuckDB, which is what happens when someone runs it
with the system Python straight after a clone.

Expect a few minutes and a few hundred megabytes of traffic: every timed run
starts from a cold DuckDB session, and the deliberately unoptimised comparison
query at the end reads a whole Parquet part.
"""

from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))


def _reexec_in_project_venv() -> None:
    """Restart under .venv/bin/python when this interpreter lacks the dependencies.

    Guarded by an environment flag so a venv that is itself incomplete fails
    with a real ImportError instead of looping.
    """
    if find_spec("duckdb") is not None or os.environ.get("GEOPARQUET_MCP_REEXEC"):
        return
    venv_python = PROJECT_DIR / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return
    os.environ["GEOPARQUET_MCP_REEXEC"] = "1"
    os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])


_reexec_in_project_venv()

from geoparquet_mcp.benchmark import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
