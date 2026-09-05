"""Both protocols, end to end, over the local corpus.

`test_app.py` makes the same claims against the remote dataset and is marked
`network` where it has to be. This file makes them where CI can check them:
the corpus is on disk, so a tool call, a REST call and the comparison between
them all run in milliseconds and cannot expire with an Overture release.

Three things are checked that only show up at this level:

  * a tool call travels the whole path — JSON-RPC over streamable HTTP, the
    SDK's argument binding, the handler, the adapter, the engine — and comes
    back with the corpus's own arithmetic in it;
  * MCP and REST give the *same* answer, because they are two façades over one
    function rather than two implementations;
  * a bad argument reaches the model as a sentence it can act on, not as a
    DuckDB binder error.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import corpus
from geoparquet_mcp.app import create_app
from geoparquet_mcp.config import ENABLE_MCP_ENV, AppConfig
from mcp_client import MCP_HEADERS, McpClient, _rpc

PLACES = corpus.PLACES
DIVISIONS = corpus.DIVISIONS
WHOLE_GRID = corpus.UNIT_SQUARE


@pytest.fixture
def client(local_config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(local_config)) as http:
        yield http


@pytest.fixture
def mcp(client: TestClient) -> McpClient:
    session = McpClient(client)
    session.initialize()
    return session


# ---------------------------------------------------------------------------
# A tool call, all the way down
# ---------------------------------------------------------------------------


def test_a_tool_call_returns_the_corpus_arithmetic(mcp: McpClient) -> None:
    """The whole path, and the answer is the number the corpus was built to give."""
    answer = mcp.tool(
        "geoparquet_aggregate_attribute",
        {"group_by": "categories.primary", "source": PLACES, **WHOLE_GRID},
    )
    assert {row["group_value"]: row["value"] for row in answer["groups"]} == (
        corpus.CATEGORY_COUNTS
    )
    assert answer["source"] == PLACES
    assert answer["release"] == corpus.FIXTURE_RELEASE


def test_a_spatial_tool_call_returns_geojson_a_client_can_render(mcp: McpClient) -> None:
    answer = mcp.tool(
        "geoparquet_filter_spatial",
        {"source": PLACES, "min_lon": 0.0, "min_lat": 0.0, "max_lon": 0.2, "max_lat": 0.2},
    )
    collection = answer["geojson"]
    assert collection["type"] == "FeatureCollection"
    assert sorted(feature["id"] for feature in collection["features"]) == [
        "g000",
        "g001",
        "g010",
        "g011",
    ]
    assert collection["features"][0]["geometry"]["type"] == "Point"


def test_the_two_dataset_tool_works_over_the_wire(mcp: McpClient) -> None:
    """`geoparquet_count_in_polygons` is the one tool that reads two sources."""
    answer = mcp.tool(
        "geoparquet_count_in_polygons",
        {"point_source": PLACES, "polygon_source": DIVISIONS, **WHOLE_GRID},
    )
    counts = {row["polygon_name"]: row["feature_count"] for row in answer["polygons"]}
    assert counts["Alpha"] == corpus.GRID_COUNT
    assert counts["Northeast"] == corpus.QUADRANT_COUNT


def test_the_catalogue_resource_advertises_exactly_the_perimeter(mcp: McpClient) -> None:
    catalogue = json.loads(
        mcp.call("resources/read", {"uri": "geoparquet://sources"})["contents"][0]["text"]
    )
    assert [entry["name"] for entry in catalogue["sources"]] == [PLACES, DIVISIONS]
    assert corpus.ELSEWHERE not in {entry["name"] for entry in catalogue["sources"]}
    assert catalogue["release"] == corpus.FIXTURE_RELEASE


def test_a_schema_is_readable_through_the_resource_template(mcp: McpClient) -> None:
    result = mcp.call("resources/read", {"uri": f"geoparquet://sources/{PLACES}/schema"})
    described = json.loads(result["contents"][0]["text"])
    assert described["row_count"] == corpus.GRID_COUNT + 6
    assert described["crs"] == "OGC:CRS84"


# ---------------------------------------------------------------------------
# One implementation, two façades
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "route", "arguments"),
    [
        (
            "geoparquet_aggregate_attribute",
            "/query/aggregate",
            {"group_by": "categories.primary", "source": PLACES, **WHOLE_GRID, "limit": 5},
        ),
        (
            "geoparquet_filter_spatial",
            "/query/spatial",
            {"source": PLACES, "min_lon": 0.0, "min_lat": 0.0, "max_lon": 0.3, "max_lat": 0.3},
        ),
        (
            "geoparquet_find_nearest",
            "/query/nearest",
            {"lon": 0.05, "lat": 0.05, "radius_km": 12.0, "source": PLACES},
        ),
        (
            "geoparquet_count_in_polygons",
            "/query/in-polygons",
            {"point_source": PLACES, "polygon_source": DIVISIONS, **WHOLE_GRID},
        ),
    ],
)
def test_mcp_and_rest_answer_identically(
    client: TestClient, mcp: McpClient, tool: str, route: str, arguments: dict
) -> None:
    """If these were two implementations they would drift, and this is where it would show.

    Everything is compared except `scan`, which is per-call by construction —
    it reports the wall-clock of one query, and two calls cannot take the same
    number of milliseconds. Identical `scan` blocks would mean the measurement
    was fabricated rather than taken.
    """
    over_mcp = mcp.tool(tool, arguments)
    response = client.get(route, params=arguments)
    assert response.status_code == 200, response.text
    over_rest = response.json()

    assert over_mcp.keys() == over_rest.keys()
    assert {k: v for k, v in over_mcp.items() if k != "scan"} == {
        k: v for k, v in over_rest.items() if k != "scan"
    }
    assert set(over_mcp["scan"]) == set(over_rest["scan"])


def test_the_perimeter_is_the_same_object_on_both_sides(client: TestClient) -> None:
    from geoparquet_mcp import dependencies

    installed = dependencies.installed()
    assert installed is not None
    assert client.get("/health").json()["perimeter"] == installed.as_dict()
    assert installed.scope.names == [DIVISIONS, PLACES]


# ---------------------------------------------------------------------------
# Refusals, as a model receives them
# ---------------------------------------------------------------------------


def _error_text(result: dict) -> str:
    assert result.get("isError"), f"expected a tool error, got: {result}"
    return result["content"][0]["text"]


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            "geoparquet_describe_source",
            {"source": corpus.ELSEWHERE},
            corpus.ELSEWHERE,
        ),
        (
            "geoparquet_aggregate_attribute",
            {"group_by": "not_a_column", "source": PLACES},
            "confidence",
        ),
        (
            "geoparquet_filter_spatial",
            {"source": PLACES, "min_lon": 1.0, "min_lat": 0.0, "max_lon": 0.0, "max_lat": 1.0},
            "min_lon must be smaller",
        ),
        (
            "geoparquet_summarize_h3",
            {"source": PLACES, **WHOLE_GRID, "resolution": 42},
            "resolution",
        ),
        (
            "geoparquet_count_in_polygons",
            {"point_source": PLACES, "polygon_source": PLACES, **WHOLE_GRID},
            "does not hold polygons",
        ),
        (
            "geoparquet_run_sql",
            {"sql": f"SELECT * FROM read_parquet('{corpus.ELSEWHERE}')"},
            "table functions are not available",
        ),
        (
            "geoparquet_run_sql",
            {"sql": f"DROP TABLE {PLACES}"},
            "only SELECT is allowed",
        ),
    ],
)
def test_a_bad_argument_comes_back_as_a_sentence_not_a_stack_trace(
    mcp: McpClient, tool: str, arguments: dict, expected: str
) -> None:
    """An engine error must survive the trip and arrive as its own text.

    The SDK draws a line: a handler raising `ToolError` is reporting an
    anticipated failure and its message is forwarded, while any other
    exception is a crash whose text stays on the server and reaches the client
    as the bare "Error executing tool X". `server.py` translates `EngineError`
    into `ToolError` for exactly this reason, and dropping that translation
    would reduce every case below to that bare line.
    """
    message = _error_text(mcp.call("tools/call", {"name": tool, "arguments": arguments}))
    assert expected in message
    assert message.strip() != f"Error executing tool {tool}", (
        "the engine's own message was lost; only the SDK's framing arrived"
    )
    # DuckDB's own diagnosis is deliberately kept — "Candidate bindings: names"
    # is useful to a model. What must never appear is a Python traceback, or a
    # path: every operation takes a dataset name and is never handed a target,
    # so an error that quoted one would give back the string the perimeter
    # exists to withhold.
    for leak in ("Traceback (most recent call last)", "read_parquet('", "LINE 1:"):
        assert leak not in message, f"an internal detail leaked to the model: {leak!r}"
    assert corpus.FIXTURE_RELEASE not in message.replace(f"release {corpus.FIXTURE_RELEASE}", "")


def test_an_out_of_scope_dataset_is_refused_over_rest_too(client: TestClient) -> None:
    response = client.get(f"/sources/{corpus.ELSEWHERE}/schema")
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "UnknownSourceError"
    assert PLACES in body["detail"], "the refusal must name what is in scope"


# ---------------------------------------------------------------------------
# The environment flag, read from the environment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "enabled"),
    [("1", True), ("true", True), ("on", True), ("0", False), ("false", False), ("off", False)],
)
def test_the_flag_decides_whether_the_mount_exists(
    monkeypatch: pytest.MonkeyPatch, registered_fixture_sources, value: str, enabled: bool
) -> None:
    """`AppConfig(mcp_enabled=...)` is already covered; this is the environment path.

    A deployment does not construct an `AppConfig` — it sets a variable. So the
    variable is what gets read here, through `from_env`, and the assertion is
    on the route table: disabled means the route does not exist, not that it
    exists and refuses.
    """
    monkeypatch.setenv(ENABLE_MCP_ENV, value)
    monkeypatch.setenv("GEOPARQUET_SOURCES", f"{PLACES},{DIVISIONS}")
    monkeypatch.setenv("GEOPARQUET_RELEASE", corpus.FIXTURE_RELEASE)
    monkeypatch.setenv("GEOPARQUET_MCP_ALLOWED_HOSTS", "testserver")

    config = AppConfig.from_env()
    assert config.mcp_enabled is enabled

    app = create_app(config)
    mounted = [route for route in app.routes if getattr(route, "path", "") == "/mcp"]
    assert bool(mounted) is enabled

    with TestClient(app) as http:
        health = http.get("/health").json()
        assert health["mcp_enabled"] is enabled
        assert health["mcp_mounted_at"] == ("/mcp" if enabled else None)
        # Either way the REST façade is whole: the two are independent.
        assert [entry["name"] for entry in http.get("/sources").json()["sources"]] == [
            PLACES,
            DIVISIONS,
        ]
        if not enabled:
            status = http.post("/mcp", json=_rpc("initialize"), headers=MCP_HEADERS).status_code
            assert status == 404


def test_a_flag_that_is_not_a_boolean_fails_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing to guess is the point: a typo must not silently disable MCP."""
    monkeypatch.setenv(ENABLE_MCP_ENV, "maybe")
    with pytest.raises(ValueError, match="is not a boolean"):
        AppConfig.from_env()
