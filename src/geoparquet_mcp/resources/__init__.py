"""MCP resources: the readable catalogue of registered remote sources."""

from __future__ import annotations

from geoparquet_mcp.resources import catalog

__all__ = ["catalog", "register_all"]


def register_all(server) -> None:
    catalog.register(server)
