"""MCP server: instantiation, tool and resource registration, transport, entry point.

This module is intentionally thin, and it is the *only* module in the package
allowed to import `mcp`. The capability lives in `engine/`, which knows
nothing about the protocol; `tools/` and `resources/` are handlers that
delegate to it. What happens here is the wiring the protocol needs, and
nothing else.
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
column names and `dataset_extent` to check the region is covered.

Then pick the tool that matches the shape of the answer you need:
  - `bbox_query` for the features themselves, as GeoJSON;
  - `nearest` for what is closest to a point, with distances;
  - `column_statistics` for "what kind of things are here", without moving them;
  - `h3_aggregate` for where they are densest;
  - `point_in_polygon` for how they distribute across administrative areas.

Always pass the tightest bounding box the question allows. The rectangle is
what makes the read cheap: it is pushed into the Parquet file and prunes whole
row groups before any byte is fetched, so a wide box costs far more than a
narrow one. Prefer an aggregate over fetching features whenever the question
allows it. Every result carries a `scan` block reporting the bytes that
actually crossed the network — read it, and tighten the box if it looks large.
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
