"""Server wiring: the tools and resources a client will actually see."""

from __future__ import annotations

from geoparquet_mcp.server import build_server

EXPECTED_TOOLS = {
    "list_sources",
    "describe_source",
    "bbox_query",
    "bbox_aggregate",
    "nearest",
    "pushdown_report",
}


async def test_every_tool_family_is_registered() -> None:
    tools = await build_server().list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_every_tool_carries_a_description() -> None:
    for tool in await build_server().list_tools():
        assert tool.description, f"{tool.name} has no description"


async def test_the_catalogue_is_exposed_as_a_resource() -> None:
    uris = {str(resource.uri) for resource in await build_server().list_resources()}
    assert "geoparquet://sources" in uris
