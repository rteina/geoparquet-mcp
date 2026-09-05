# geoparquet-mcp

An MCP server that runs spatial analysis directly on remote GeoParquet files — DuckDB reads them
over HTTP range requests, so nothing is downloaded or imported first.

```sh
./scripts/demo.sh
```

The script prepares its own environment: [uv](https://docs.astral.sh/uv/) when it is on PATH,
otherwise `python3 -m venv` plus pip on Python 3.12+. Run `./scripts/demo.sh --help` for options.
