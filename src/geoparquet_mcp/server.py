"""MCP server: instantiation, tool and resource registration, transport, entry point.

This module is intentionally thin, and it is the *only* module in the package
allowed to import `mcp`. The capability lives in `engine/`, which knows
nothing about the protocol; `tools/` and `resources/` are handlers that
delegate to it, and `dependencies.py` is the adapter that hands them a
perimeter without either side importing the other. What happens here is the
wiring the protocol needs, and nothing else.

Two ways in:

  * `mcp_asgi_app()` returns the Starlette application for the streamable-HTTP
    transport, which `app.py` mounts inside FastAPI. That is the main path.
  * `main()` runs the server standalone, defaulting to stdio, which is the
    transport a desktop client launches as a subprocess.

The stdio path resolves and installs the dependencies itself, because there is
no FastAPI lifespan to do it. That is deliberate duplication of two lines
rather than a shared "framework": the alternative is a handler that silently
falls back to the default perimeter when nobody installed one, which is the
failure `dependencies.py` exists to make impossible.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from geoparquet_mcp import __version__, dependencies, resources, tools
from geoparquet_mcp.config import AppConfig
from geoparquet_mcp.engine.errors import EngineError

INSTRUCTIONS = """\
Spatial analysis over remote GeoParquet files, read in place by DuckDB.

Nothing is imported or downloaded first: each tool issues HTTP range requests
against public object storage and reads only the row groups and columns its
filters need. A 10 GB dataset is queryable in seconds without a database.

Start by reading the `geoparquet://sources` resource — the catalogue of
datasets, and the names every tool accepts. Then call
`geoparquet_describe_source` to learn the real column names, the coordinate
system and the extent the dataset covers. Overture nests its columns, so a
category is `categories.primary` and a label is `names.primary`; guessing them
costs a wasted call.

Then pick the tool that matches the shape of the answer you need:
  - `geoparquet_preview_rows` to see what the values look like before filtering;
  - `geoparquet_filter_spatial` for the features themselves, as GeoJSON, inside
    a rectangle or an arbitrary WKT geometry;
  - `geoparquet_find_nearest` for what is closest to a point, with distances;
  - `geoparquet_aggregate_attribute` for "how many of each" and "what is the
    average", computed remotely;
  - `geoparquet_summarize_h3` for where things are densest;
  - `geoparquet_run_sql` last, for a join or a window function the others
    cannot express.

Two habits decide whether a question costs kilobytes or gigabytes.

Always pass the tightest bounding box the question allows. The rectangle is
what makes the read cheap: it is pushed into the Parquet file and prunes whole
row groups before any byte is fetched, so a wide box costs far more than a
narrow one.

Prefer an aggregate over fetching features whenever the question allows it.
Counting a million places remotely returns a few rows; fetching them to count
them locally returns a million.

Every result carries a `scan` block reporting the bytes that actually crossed
the network — read it, and tighten the query if it looks large.
"""


class _SurfacingEngineErrors:
    """Registers handlers so an engine error reaches the model as its own text.

    The SDK draws a deliberate line: a handler that raises `ToolError` is
    reporting an anticipated failure and its message is sent to the client,
    while anything else is a crash whose text stays on the server and reaches
    the client as "Error executing tool X". That line is right, and the engine
    sits on the near side of it — `InvalidRequestError`, `UnknownSourceError`
    and `UnknownColumnError` carry messages written for a model to read and act
    on ("call the schema operation to list the columns this dataset has"), and
    losing them would turn a fixable mistake into a dead end.

    The translation belongs here rather than in the handlers, because
    `ToolError` is a protocol type and `tools/` may not import the protocol.
    This proxy stands in for the server during registration and does nothing
    else; `functools.wraps` keeps the original signature, which is what the
    SDK reads to build each tool's input schema.
    """

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def _wrapping(self, register: Callable[..., Any], failure: type[Exception]) -> Callable:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(handler)
            def surfaced(*args: Any, **kwargs: Any) -> Any:
                try:
                    return handler(*args, **kwargs)
                except EngineError as exc:
                    raise failure(str(exc)) from exc

            return register(surfaced)

        return decorate

    def tool(self, *args: Any, **kwargs: Any) -> Callable:
        return self._wrapping(self._server.tool(*args, **kwargs), ToolError)

    def resource(self, *args: Any, **kwargs: Any) -> Callable:
        return self._wrapping(self._server.resource(*args, **kwargs), ResourceError)


def build_server() -> MCPServer:
    """Create the server with every tool and resource attached."""
    server = MCPServer(
        name="geoparquet-mcp",
        title="Remote GeoParquet spatial analysis",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    registrar = _SurfacingEngineErrors(server)
    tools.register_all(registrar)
    resources.register_all(registrar)
    return server


def mcp_asgi_app(
    server: MCPServer,
    allowed_hosts: tuple[str, ...] | list[str] = (),
) -> StreamableHTTPASGIApp:
    """The streamable-HTTP transport as a bare ASGI application, ready to mount.

    Mounting the Starlette wrapper the SDK returns would put the endpoint at
    `/mcp/` and leave `/mcp` answering only through a 307 — its inner router
    matches on the path left after the mount prefix is stripped, and that is
    the empty string. Mounting the ASGI application the wrapper contains skips
    that inner routing entirely, so `POST /mcp` is the endpoint and not a
    redirect to it.

    `streamable_http_app()` is still what builds and configures the session
    manager, which the SDK exposes for exactly this: "advanced use cases like
    mounting multiple MCPServer instances in a single FastAPI application".
    The Starlette object it returns is then discarded.

    `allowed_hosts` extends the SDK's DNS-rebinding protection, which by
    default accepts only localhost. Anything else — a container name, a host
    behind a proxy, a test client — has to be named, and is refused with 421
    until it is.
    """
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[*allowed_hosts, "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                *(f"http://{host}" for host in allowed_hosts),
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )
    server.streamable_http_app(streamable_http_path="/", transport_security=security)
    return StreamableHTTPASGIApp(server.session_manager)


def mcp_lifespan(server: MCPServer) -> AbstractAsyncContextManager[None]:
    """The session manager's lifecycle, for the host application to enter.

    A mounted ASGI application gets no startup event of its own, so whoever
    mounts `mcp_asgi_app()` must hold this open for as long as the mount
    exists. Skip it and every request to the endpoint fails on a session
    manager that was never started.
    """
    return server.session_manager.run()


def main(argv: list[str] | None = None) -> None:
    """Run the server standalone. Defaults to stdio, the transport MCP clients launch."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="geoparquet-mcp-server",
        description="MCP server for spatial analysis over remote GeoParquet files.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport to serve on (default: stdio).",
    )
    args = parser.parse_args(argv)
    transport: Literal["stdio", "sse", "streamable-http"] = args.transport

    # No FastAPI lifespan here, so the perimeter is resolved and installed by
    # the entry point instead. Same object, same guarantee.
    dependencies.install(dependencies.resolve(AppConfig.from_env()))
    build_server().run(transport=transport)


if __name__ == "__main__":
    main()
