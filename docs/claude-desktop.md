# Running this server in Claude Desktop

Claude Desktop launches an MCP server as a subprocess and talks to it over
stdin/stdout. That is the STDIO transport, and it is what the configuration
below selects. The HTTP transport described at the bottom exists for
everything else — MCP Inspector, a service, another agent — and is the same
server in the same process as the REST API.

## Install

```bash
git clone https://github.com/rteina/geoparquet-mcp
cd geoparquet-mcp
uv sync            # or: python -m venv .venv && .venv/bin/pip install -e .
```

This installs the `geoparquet-mcp-server` entry point into the virtual
environment. Claude Desktop does not read your shell profile, so the
configuration has to name that executable by absolute path — `geoparquet-mcp-server`
alone will not resolve.

Find the path:

```bash
echo "$PWD/.venv/bin/geoparquet-mcp-server"
```

Or skip both steps: `./scripts/serve.sh config` prepares `.venv/` if it is not
there yet and prints the whole block below with the absolute path already
filled in.

## Configure

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` on
macOS (`%APPDATA%\Claude\claude_desktop_config.json` on Windows), and add:

```json
{
  "mcpServers": {
    "geoparquet": {
      "command": "/absolute/path/to/geoparquet-mcp/.venv/bin/geoparquet-mcp-server",
      "args": ["--transport", "stdio"]
    }
  }
}
```

Restart Claude Desktop. The eight tools appear under the connector.

**This exact configuration was tested** — not by hand in the desktop app, but
by launching the command from this file as a subprocess and driving it with an
MCP client over its pipes, which is the same thing Claude Desktop does. The
server initialised, listed eight tools and the `geoparquet://sources`
resource, and answered a real query against the 73.6-million-row Overture
`places` dataset. What has *not* been verified is the desktop UI itself:
whether the connector renders the way you expect is between you and the app.

### Optional settings

Each is an environment variable, so each goes in an `"env"` block:

```json
{
  "mcpServers": {
    "geoparquet": {
      "command": "/absolute/path/to/.venv/bin/geoparquet-mcp-server",
      "args": ["--transport", "stdio"],
      "env": {
        "GEOPARQUET_SOURCES": "overture_places",
        "GEOPARQUET_RELEASE": "2026-08-19.0"
      }
    }
  }
}
```

| Variable | Default | What it does |
|---|---|---|
| `GEOPARQUET_SOURCES` | every registered dataset | Comma-separated dataset names. Narrows the perimeter for the whole process: a dataset left out has no path, so no tool can reach it. |
| `GEOPARQUET_RELEASE` | newest on the bucket | Pins an Overture release. Overture keeps only the last two, so a pin from a few months ago will 404 — leave it unset and the newest is discovered at startup. |
| `GEOPARQUET_ENABLE_MCP` | `1` | Only meaningful for the HTTP server below. Under STDIO the process *is* the MCP server. |

## First things to ask

The server ships instructions telling the model how to use it, but a cold
start is smoother if you begin with orientation rather than a query:

- *"What datasets can you query, and how big are they?"* — reads the
  catalogue resource, no tool call.
- *"What columns does the places dataset have, and what area does it cover?"*
  — `geoparquet_describe_source`. Worth doing before anything else, because
  Overture nests its columns (`categories.primary`, not `category`).
- *"How many places of each category are in central Paris?"* —
  `geoparquet_aggregate_attribute`, which counts inside the remote file and
  returns a few rows.

Every answer carries a `scan` block reporting the bytes that actually crossed
the network. Asking the model to quote it is a good way to see what the
project is claiming.

### What the first query costs

The first call in a fresh process reads Parquet footers over HTTP — about
26.5 MB for `overture_places`, whose 16 parts carry 4096 row groups of
statistics — and takes a few seconds. Every later call reuses those cached
footers and reads only data pages, or nothing at all. So a slow first
question and instant follow-ups is the expected shape, not a fault.

## Troubleshooting

**The connector does not appear.** The JSON is almost always the problem: a
trailing comma, or a relative `command`. Claude Desktop's logs are in
`~/Library/Logs/Claude/mcp*.log`.

**It appears but every call fails.** Check the command runs on its own:

```bash
/absolute/path/to/.venv/bin/geoparquet-mcp-server --transport stdio
# from a clone, the same thing: ./scripts/serve.sh stdio
```

It should sit silently waiting for JSON-RPC on stdin. If it exits with a
traceback instead, the environment is the problem, not the configuration.
A server that starts and lists its tools but fails every call is a different
fault: the perimeter was never installed. Both stdio entry points resolve and
install it before serving, and `tests/test_cli.py` drives each of them over its
own pipes to keep that true.

**Queries fail with an HTTP 404 from the bucket.** The pinned Overture release
has expired. Remove `GEOPARQUET_RELEASE` and the server discovers the current
one at startup.

## The HTTP transport

The same server also runs as an ASGI sub-application inside the FastAPI app,
in one process, sharing its DuckDB session:

```bash
./scripts/serve.sh http --port 8000
# without the script: uvicorn geoparquet_mcp.app:create_app --factory --port 8000
```

- MCP, streamable HTTP: `POST http://127.0.0.1:8000/mcp`
- REST, the same operations: `GET /sources`, `/query/spatial`, `/query/aggregate`, …
- `GET /health` reports where MCP is mounted, or `null` when it is switched off.

Set `GEOPARQUET_ENABLE_MCP=0` and the sub-application is never built and the
route is never registered — `/mcp` returns 404 because nothing is there, while
the REST side keeps serving.

Point MCP Inspector at it with:

```bash
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/mcp \
  --transport http --method tools/list
```

The transport refuses a `Host` header it does not recognise, with HTTP 421 —
DNS-rebinding protection, on by default for localhost. Behind a proxy or in a
container, name the host you are reached by:

```bash
GEOPARQUET_MCP_ALLOWED_HOSTS=geoparquet.internal:8000 uvicorn ...
```
