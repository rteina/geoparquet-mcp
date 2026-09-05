"""MCP server: instantiation, tool and resource registration, transport, entry point.

This module is intentionally thin. The behaviour lives in `tools/` and
`resources/`; what happens here is the wiring the protocol needs.
"""

from __future__ import annotations

import argparse
from typing import Literal

from mcp.server.mcpserver import MCPServer

from geoparquet_mcp import __version__, resources, tools

INSTRUCTIONS = """\
Spatial analysis over remote GeoParquet files, read in place by DuckDB.

Nothing is imported or downloaded first: each tool issues HTTP range requests
against public object storage and reads only the row groups and columns its
filters need. Start with `list_sources`, then `describe_source` to learn the
column names, then `bbox_query`, `bbox_aggregate` or `nearest`.

Always pass the tightest bounding box the question allows. The rectangle is
what makes the read cheap; a wide box reads a lot more of the file. Every
result carries a `scan` block reporting the bytes that actually crossed the
network, and `pushdown_report` measures that saving directly.
"""


def build_server() -> MCPServer:
    """Create the server with every tool and resource attached."""
    server = MCPServer(
        name="geoparquet-mcp",
        title="Remote GeoParquet spatial analysis",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    tools.register_all(server)
    resources.register_all(server)
    return server


def main(argv: list[str] | None = None) -> None:
    """Run the server. Defaults to stdio, the transport MCP clients launch."""
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
    build_server().run(transport=transport)


if __name__ == "__main__":
    main()
