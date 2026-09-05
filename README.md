# geoparquet-mcp

[![CI](https://github.com/rteina/geoparquet-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/rteina/geoparquet-mcp/actions/workflows/ci.yml)

An MCP server that runs spatial analysis directly on remote GeoParquet files — DuckDB reads them
over HTTP range requests, so nothing is downloaded or imported first.

```sh
./scripts/demo.sh
```

The script prepares its own environment: [uv](https://docs.astral.sh/uv/) when it is on PATH,
otherwise `python3 -m venv` plus pip on Python 3.12+. Run `./scripts/demo.sh --help` for options.

## Tests

```sh
uv run pytest -m "not network"   # what CI runs: hermetic, on a generated local corpus
uv run pytest -m network         # the same claims against the live Overture dataset
```

The suite is split because the two halves fail for different reasons. The default half
generates a tiny Parquet corpus on disk (`tests/corpus.py`, or `./scripts/make_fixtures.py` to
inspect it) and asserts exact results in seconds; the `network` half reads Overture's public
bucket, is the only place the byte-level pushdown claim can be measured, and breaks whenever a
release expires — so it runs on a schedule rather than on every push.
