"""The smallest streamable-HTTP MCP client that can hold a session.

Shared by every test that speaks the protocol over the wire rather than
calling `build_server()` in process. It exists because the two are different
claims: one checks what the server would answer, the other checks what a
client actually receives through the transport the deployment uses.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

PROTOCOL_VERSION = "2025-06-18"

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
    """One MCP session against a mounted server, over streamable HTTP."""

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
