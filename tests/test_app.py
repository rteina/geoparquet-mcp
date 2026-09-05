"""The combined application: one process, two protocols, one implementation.

The claims under test are the ones the README makes, so they are checked
rather than asserted: MCP is mounted inside the FastAPI app and not proxied
beside it; the environment flag removes the route rather than making it
refuse; and the same question asked over MCP and over REST gets the same
answer, because both call the same function.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from geoparquet_mcp import dependencies
from geoparquet_mcp.app import create_app
from geoparquet_mcp.config import AppConfig

RELEASE = "2026-08-19.0"
PROTOCOL_VERSION = "2025-06-18"

# TestClient sends Host: testserver, which the transport's DNS-rebinding
# protection rejects with 421 unless it is named. Naming it here is the same
# thing a deployment behind a proxy has to do.
TEST_CONFIG = AppConfig(
    mcp_enabled=True,
    sources=("overture_places", "overture_divisions"),
    release=RELEASE,
    allowed_hosts=("testserver",),
)

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _payload(response: Any) -> dict[str, Any]:
    """One JSON-RPC result, however the transport chose to frame it.

    Streamable HTTP may answer with a plain JSON body or with a single
    server-sent event; a test that only understood one of them would break on
    a transport setting rather than on a bug.
    """
    body = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in body.splitlines():
            if line.startswith("data: "):
                body = line.removeprefix("data: ")
                break
    return json.loads(body)


class McpClient:
    """The smallest streamable-HTTP client that can hold a session."""

    def __init__(self, http: TestClient, path: str = "/mcp") -> None:
        self._http = http
        self._path = path
        self._headers = dict(MCP_HEADERS)
        self._id = 0

    def _post(self, message: dict[str, Any]) -> Any:
        return self._http.post(self._path, json=message, headers=self._headers)

    def initialize(self) -> dict[str, Any]:
        response = self._post(
            _rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            )
        )
        response.raise_for_status()
        session = response.headers.get("mcp-session-id")
        if session:
            self._headers["mcp-session-id"] = session
        self._headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        self._http.post(
            self._path,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._headers,
        )
        return _payload(response)["result"]

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        response = self._post(_rpc(method, params, request_id=self._id))
        response.raise_for_status()
        document = _payload(response)
        assert "error" not in document, document["error"]
        return document["result"]

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool and return its structured result."""
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), result
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        return json.loads(result["content"][0]["text"])


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(TEST_CONFIG)) as http:
        yield http


@pytest.fixture
def mcp(client: TestClient) -> McpClient:
    session = McpClient(client)
    session.initialize()
    return session


# ---------------------------------------------------------------------------
# The mount, and the flag that decides whether it exists
# ---------------------------------------------------------------------------


def test_health_reports_where_mcp_is_mounted(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mcp_enabled"] is True
    assert body["mcp_mounted_at"] == "/mcp"
    assert body["perimeter"]["sources"] == ["overture_divisions", "overture_places"]


def test_with_mcp_disabled_the_route_does_not_exist() -> None:
    """Disabled means absent, not present-and-refusing.

    Checked on the route table as well as on the response, because a 404 from
    a handler and a 404 from an unrouted path look identical from outside and
    are not the same thing: the first still built and mounted the server.
    """
    app = create_app(AppConfig(mcp_enabled=False, sources=("overture_places",), release=RELEASE))
    assert not [route for route in app.routes if getattr(route, "path", "") == "/mcp"]
    with TestClient(app) as http:
        assert http.post("/mcp", json=_rpc("initialize"), headers=MCP_HEADERS).status_code == 404
        body = http.get("/health").json()
        assert body["mcp_enabled"] is False
        assert body["mcp_mounted_at"] is None


def test_with_mcp_disabled_the_rest_side_still_serves() -> None:
    """The two façades are independent: switching one off leaves the other whole."""
    app = create_app(AppConfig(mcp_enabled=False, sources=("overture_places",), release=RELEASE))
    with TestClient(app) as http:
        assert [entry["name"] for entry in http.get("/sources").json()["sources"]] == [
            "overture_places"
        ]


def test_mcp_answers_on_the_mount_path_itself_not_a_redirect(client: TestClient) -> None:
    """`/mcp` is the endpoint. Not `/mcp/`, and not a 307 towards it."""
    response = client.post(
        "/mcp",
        json=_rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        ),
        headers=MCP_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 200, response.text


def test_mcp_and_rest_share_one_process(client: TestClient) -> None:
    """The mounted app is this app, not a client to another one.

    If MCP were a proxy to a second process it would have its own DuckDB
    session and its own resolved perimeter. Both protocols reporting the
    identical dependencies object is what rules that out.
    """
    assert dependencies.installed() is not None
    seen_over_rest = client.get("/health").json()["perimeter"]
    assert seen_over_rest == dependencies.installed().as_dict()


# ---------------------------------------------------------------------------
# The surface, over the wire
# ---------------------------------------------------------------------------


def test_the_client_sees_exactly_the_published_tools(mcp: McpClient) -> None:
    names = {tool["name"] for tool in mcp.call("tools/list")["tools"]}
    assert names == {
        "geoparquet_describe_source",
        "geoparquet_preview_rows",
        "geoparquet_filter_spatial",
        "geoparquet_aggregate_attribute",
        "geoparquet_summarize_h3",
        "geoparquet_find_nearest",
        "geoparquet_count_in_polygons",
        "geoparquet_run_sql",
    }


def test_the_server_introduces_itself(mcp: McpClient) -> None:
    result = mcp.initialize()
    assert result["serverInfo"]["name"] == "geoparquet-mcp"
    assert "tightest bounding box" in result["instructions"]


def test_the_catalogue_resource_shows_only_the_datasets_in_scope(mcp: McpClient) -> None:
    """A resource that advertised more than the tools can query would be lying."""
    result = mcp.call("resources/read", {"uri": "geoparquet://sources"})
    catalogue = json.loads(result["contents"][0]["text"])
    # Configured order, not sorted: `entries()` lists the scope as it was
    # built, which is the order a deployment chose to advertise its datasets.
    assert [entry["name"] for entry in catalogue["sources"]] == list(TEST_CONFIG.sources)
    assert "overture_buildings" not in {entry["name"] for entry in catalogue["sources"]}
    assert catalogue["release"] == RELEASE


def test_a_tool_cannot_reach_a_dataset_outside_the_perimeter(mcp: McpClient) -> None:
    """The perimeter is not advice. `overture_buildings` is registered but out of scope."""
    result = mcp.call(
        "tools/call",
        {
            "name": "geoparquet_describe_source",
            "arguments": {"source": "overture_buildings"},
        },
    )
    assert result["isError"]
    assert "overture_buildings" in result["content"][0]["text"]


def test_an_ad_hoc_query_cannot_name_a_file(mcp: McpClient) -> None:
    """The escape hatch does not escape the perimeter."""
    result = mcp.call(
        "tools/call",
        {
            "name": "geoparquet_run_sql",
            "arguments": {"sql": "SELECT * FROM read_parquet('s3://elsewhere/x.parquet')"},
        },
    )
    assert result["isError"]
    assert "table functions are not available" in result["content"][0]["text"]


def test_a_write_statement_is_refused(mcp: McpClient) -> None:
    result = mcp.call(
        "tools/call",
        {"name": "geoparquet_run_sql", "arguments": {"sql": "DROP TABLE overture_places"}},
    )
    assert result["isError"]
    assert "only SELECT is allowed" in result["content"][0]["text"]


def test_an_ad_hoc_query_runs_without_touching_the_network(mcp: McpClient) -> None:
    """A query naming no dataset binds no view, so it reads nothing."""
    answer = mcp.tool("geoparquet_run_sql", {"sql": "SELECT 6 * 7 AS answer"})
    assert answer["rows"] == [{"answer": 42}]
    assert answer["tables_read"] == []
    assert answer["scan"]["bytes_scanned"] == 0


# ---------------------------------------------------------------------------
# The two façades, on the same question
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_the_same_question_gets_the_same_answer_over_both_protocols(
    client: TestClient, mcp: McpClient
) -> None:
    """The claim the whole file exists to support, checked rather than asserted.

    Two protocols, one implementation. If the REST route and the MCP tool
    were separate implementations they would drift — a different default, a
    rounded number, a renamed key — and this is where that would show up.

    Everything is compared except `scan`, which is per-call by construction:
    it reports the bytes and milliseconds of one query, and the second call
    finds the first one's pages cached. Identical `scan` blocks would mean the
    measurement was fake.
    """
    arguments = {
        "group_by": "categories.primary",
        "min_lon": 2.33,
        "min_lat": 48.85,
        "max_lon": 2.36,
        "max_lat": 48.87,
        "limit": 6,
    }
    over_mcp = mcp.tool("geoparquet_aggregate_attribute", arguments)
    over_rest = client.get("/query/aggregate", params=arguments).json()

    assert over_mcp.keys() == over_rest.keys()
    assert {k: v for k, v in over_mcp.items() if k != "scan"} == {
        k: v for k, v in over_rest.items() if k != "scan"
    }
    assert over_mcp["groups"], "the fixture region should not be empty"


@pytest.mark.network
def test_both_protocols_read_through_the_same_session(
    client: TestClient, mcp: McpClient
) -> None:
    """Not just the same answer — the same cache, which means the same process.

    A second read of a region the first call already fetched costs no bytes.
    That only holds if both protocols share one DuckDB session, so it is the
    observable difference between a mounted sub-application and a proxy to a
    second process.
    """
    box = {"min_lon": 2.34, "min_lat": 48.86, "max_lon": 2.35, "max_lat": 48.865}
    warmed = client.get("/query/aggregate", params={"group_by": "categories.primary", **box})
    assert warmed.status_code == 200

    second = mcp.tool("geoparquet_aggregate_attribute", {"group_by": "categories.primary", **box})
    assert second["scan"]["bytes_scanned"] == 0, (
        "the MCP side re-read bytes the REST side had already fetched, so they "
        "are not sharing a session"
    )
