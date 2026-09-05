# geoparquet-mcp

An MCP server that runs spatial analysis directly on remote GeoParquet files — DuckDB reads them
over HTTP range requests, so nothing is downloaded or imported first.

```sh
uv run geoparquet-mcp demo
```

Without [uv](https://docs.astral.sh/uv/), on Python 3.12+:

```sh
pip install -e . && geoparquet-mcp demo
```
