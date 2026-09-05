"""Server wiring: the tools and resources a client will actually see."""

from __future__ import annotations

from geoparquet_mcp.server import build_server

EXPECTED_TOOLS = {
    "list_sources",
    "describe_source",
    "dataset_extent",
    "bbox_query",
    "nearest",
    "column_statistics",
    "h3_aggregate",
    "point_in_polygon",
}


async def test_every_tool_family_is_registered() -> None:
    tools = await build_server().list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_the_measuring_bench_is_not_an_agent_capability() -> None:
    """`pushdown_report` proves the project's claim; it is not a question about a map.

    It lives in `geoparquet_mcp.benchmark`, where it can be run and quoted,
    rather than in the tool surface, where an agent would eventually call it
    and pull a whole Parquet part over the network to no purpose.
    """
    names = {tool.name for tool in await build_server().list_tools()}
    assert "pushdown_report" not in names


async def test_every_tool_carries_a_description() -> None:
    for tool in await build_server().list_tools():
        assert tool.description, f"{tool.name} has no description"


async def test_the_catalogue_is_exposed_as_a_resource() -> None:
    uris = {str(resource.uri) for resource in await build_server().list_resources()}
    assert "geoparquet://sources" in uris
