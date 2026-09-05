"""Server wiring: the tools and resources a client will actually see."""

from __future__ import annotations

import pytest

from geoparquet_mcp.server import build_server

# The whole agent-facing surface, named. A tool is a thing a model has to
# choose between, so the list is short on purpose and this test is what keeps
# it short: adding one means changing this line and having a reason.
EXPECTED_TOOLS = {
    "geoparquet_describe_source",
    "geoparquet_preview_rows",
    "geoparquet_filter_spatial",
    "geoparquet_aggregate_attribute",
    "geoparquet_summarize_h3",
    "geoparquet_find_nearest",
    "geoparquet_count_in_polygons",
    "geoparquet_run_sql",
}


async def test_every_tool_family_is_registered() -> None:
    tools = await build_server().list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_the_surface_stays_small_enough_to_choose_from() -> None:
    """Eight is the ceiling, and the eighth was argued for rather than added.

    A tool list is a menu a model reads on every turn. Past a handful the
    choice gets worse, not better, and the marginal tool is usually a special
    case of one already there. That is why the ceiling exists.

    `geoparquet_count_in_polygons` is the exception, and the reason it earns
    the slot is that it is not a special case of anything here: it is the only
    tool that reads two datasets at once, and the only way to group by a shape
    rather than by a column. It is reachable through `geoparquet_run_sql`, but
    only by a caller that writes a correct spatial join with bbox predicates
    on both sides — which is exactly the pruning a typed tool guarantees and
    an ad-hoc query does not.

    Nine would need a better argument than that one.
    """
    assert len(await build_server().list_tools()) <= 8


async def test_the_measuring_bench_is_not_an_agent_capability() -> None:
    """`pushdown_report` proves the project's claim; it is not a question about a map.

    It lives in `geoparquet_mcp.benchmark`, where it can be run and quoted,
    rather than in the tool surface, where an agent would eventually call it
    and pull a whole Parquet part over the network to no purpose. Every
    spatial tool already reports its own bytes, which is the part an agent can
    act on.
    """
    names = {tool.name for tool in await build_server().list_tools()}
    assert "pushdown_report" not in names


async def test_the_catalogue_is_a_resource_not_a_tool() -> None:
    """Listing datasets is a document to read, not a call to make.

    MCP has a primitive for "state a client can pull into context without
    invoking anything", and a catalogue is exactly that. Spending a tool slot
    on it would cost an agent a round trip for something it could have been
    handed.
    """
    server = build_server()
    assert "list_sources" not in {tool.name for tool in await server.list_tools()}
    assert "geoparquet://sources" in {str(r.uri) for r in await server.list_resources()}


async def test_a_dataset_schema_is_readable_as_a_resource() -> None:
    templates = {t.uri_template for t in await build_server().list_resource_templates()}
    assert "geoparquet://sources/{source}/schema" in templates


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
async def test_every_tool_description_is_written_for_a_model(tool_name: str) -> None:
    """Descriptions are the interface, and the reader is a language model.

    Checked for the three things a model actually needs and a one-line
    description never has: when to reach for this tool, what the parameters
    mean, and what comes back.
    """
    tool = next(t for t in await build_server().list_tools() if t.name == tool_name)
    description = tool.description or ""
    assert len(description) > 400, f"{tool_name}: description is too thin to choose from"
    for heading in ("WHEN TO USE IT", "PARAMETERS", "WHAT COMES BACK"):
        assert heading in description, f"{tool_name}: description never says {heading.lower()}"


async def test_every_tool_takes_a_documented_input_schema() -> None:
    for tool in await build_server().list_tools():
        schema = tool.input_schema
        assert schema["type"] == "object", tool.name
        assert "properties" in schema, tool.name
