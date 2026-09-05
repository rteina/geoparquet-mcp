"""Application configuration, read once from the environment.

Everything that decides what the process does — whether MCP is mounted, which
datasets are in scope, which release to pin — is resolved here, at startup,
into one frozen object. Nothing downstream reads `os.environ`, so what the
process is doing can be printed rather than inferred.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# The flag that decides whether this process speaks MCP at all. When it is
# off, the sub-application is never constructed and the route is never
# registered — `/mcp` is not a 404 handler, it is nothing.
ENABLE_MCP_ENV = "GEOPARQUET_ENABLE_MCP"

# Comma-separated dataset names. Absent means every registered source.
SOURCES_ENV = "GEOPARQUET_SOURCES"

# Overture release to pin. Absent means resolve the newest available.
RELEASE_ENV = "GEOPARQUET_RELEASE"

# Where the MCP sub-application is mounted inside the FastAPI app.
MCP_PATH_ENV = "GEOPARQUET_MCP_PATH"

# Comma-separated Host header values the MCP transport will accept, on top of
# the SDK's localhost defaults. Needed behind a proxy or under a test client.
ALLOWED_HOSTS_ENV = "GEOPARQUET_MCP_ALLOWED_HOSTS"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _flag(name: str, default: bool) -> bool:
    """Read a boolean environment variable, refusing to guess at nonsense."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(
        f"{name}={raw!r} is not a boolean; use one of "
        f"{', '.join(sorted(_TRUE))} or {', '.join(sorted(_FALSE))}"
    )


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class AppConfig:
    """What this process serves, and over what.

    `sources` empty means every registered dataset; a non-empty list narrows
    the perimeter for the whole process, and nothing downstream can widen it
    again.
    """

    mcp_enabled: bool = True
    mcp_path: str = "/mcp"
    sources: tuple[str, ...] = ()
    release: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> AppConfig:
        """Build the configuration this process will run under."""
        path = os.environ.get(MCP_PATH_ENV, "/mcp").strip() or "/mcp"
        if not path.startswith("/") or path == "/":
            raise ValueError(f"{MCP_PATH_ENV}={path!r} must be an absolute path such as '/mcp'")
        return cls(
            mcp_enabled=_flag(ENABLE_MCP_ENV, default=True),
            mcp_path=path.rstrip("/"),
            sources=tuple(_csv(SOURCES_ENV)),
            release=os.environ.get(RELEASE_ENV) or None,
            allowed_hosts=tuple(_csv(ALLOWED_HOSTS_ENV)),
        )

    def as_dict(self) -> dict[str, Any]:
        """The configuration as plain data, for `/health` and for logging."""
        return {
            "mcp_enabled": self.mcp_enabled,
            "mcp_path": self.mcp_path if self.mcp_enabled else None,
            "sources": list(self.sources) or "all registered",
            "release": self.release or "resolved at startup",
        }
