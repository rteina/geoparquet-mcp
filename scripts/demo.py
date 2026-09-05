#!/usr/bin/env python3
"""Run the geoparquet-mcp demo without installing an entry point.

Equivalent to `geoparquet-mcp demo`. Kept so that `python scripts/demo.py`
works from a checkout, for anyone who prefers that to a console script.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from geoparquet_mcp.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["demo", *sys.argv[1:]]))
