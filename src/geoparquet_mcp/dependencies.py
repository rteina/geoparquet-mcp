"""The bridge between a request context and the engine's dependencies.

The engine's operations each take an optional `scope` and `session`. Left out,
they fall back to process-wide defaults — convenient in a notebook, wrong in a
server: it means every handler decides for itself what it is allowed to read,
and "what can this deployment see?" stops having one answer.

This module makes it have one. The perimeter is resolved once, when the
application starts, and installed here. A handler asks for what is installed
and gets a frozen `EngineDependencies`; it has no argument through which a
different scope could arrive and no constructor to call, so the strongest
thing it can do to the perimeter is narrow it for itself. Widening is not
refused at runtime — it is unreachable.

The perimeter is stored in two layers — a process default and a per-context
override — for reasons spelled out where they are defined. The short version:
the default has to outlive the startup task that set it, and the override has
to not leak between concurrent requests.

This file deliberately imports neither `mcp` nor `fastapi`. It is the adapter
between protocol handlers and the engine, and an adapter that knows both sides
is just coupling with an extra file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from geoparquet_mcp.config import AppConfig
from geoparquet_mcp.engine import DatasetScope, Session, get_session


class DependenciesNotInstalledError(RuntimeError):
    """A handler ran outside an application that had resolved a perimeter.

    Raised rather than quietly falling back to the default scope: a silent
    fallback would mean a misconfigured deployment reads every registered
    dataset instead of the ones it was restricted to, which is the failure
    this module exists to prevent.
    """


@dataclass(frozen=True)
class EngineDependencies:
    """The perimeter and the connection a handler is allowed to use.

    Frozen, and carrying no way to reach a wider scope: `DatasetScope` can
    only be narrowed, and the session is a connection, not a policy.
    """

    scope: DatasetScope
    session: Session

    @property
    def kwargs(self) -> dict[str, Any]:
        """The two arguments every engine operation accepts, ready to splat."""
        return {"scope": self.scope, "session": self.session}

    def narrowed_to(self, names: list[str]) -> EngineDependencies:
        """The same session over fewer datasets. Never more — `DatasetScope` refuses."""
        return EngineDependencies(scope=self.scope.narrowed_to(names), session=self.session)

    def as_dict(self) -> dict[str, Any]:
        """What perimeter this is, as plain data."""
        return {"release": self.scope.release, "sources": self.scope.names}


def resolve(config: AppConfig | None = None) -> EngineDependencies:
    """Build the process's dependencies from its configuration.

    Called once, by the application. Resolving the scope touches the network
    once to work out the current Overture release; the session is the
    process-wide DuckDB connection, whose loaded extensions and cached Parquet
    footers are the reason it is shared rather than rebuilt per request.
    """
    config = config or AppConfig()
    if config.sources:
        scope = DatasetScope.restricted_to(config.sources, release=config.release)
    else:
        scope = DatasetScope.default(release=config.release)
    return EngineDependencies(scope=scope, session=get_session())


# Two places, deliberately, because they answer two different questions.
#
# `_INSTALLED` is the process's perimeter: one value, set once at startup by
# whoever runs the application. It has to be a plain global rather than a
# ContextVar, because a ContextVar set inside the lifespan task is invisible to
# the request tasks — those copy the context they were created in, not the one
# startup ran in. That failure is silent: every handler simply finds nothing
# installed.
#
# `_OVERRIDE` is a narrower perimeter for the current context only, which is
# what `using()` sets. It wins when present, so a per-request scope — a tenant,
# an API key with fewer datasets — is one `with` block away and cannot leak
# into a concurrent request.
_INSTALLED: EngineDependencies | None = None
_OVERRIDE: ContextVar[EngineDependencies | None] = ContextVar(
    "geoparquet_engine_dependencies", default=None
)


def install(dependencies: EngineDependencies) -> None:
    """Make these dependencies the process default, for the application's life."""
    global _INSTALLED
    _INSTALLED = dependencies


def clear() -> None:
    """Forget the process default. Called when the application shuts down."""
    global _INSTALLED
    _INSTALLED = None


def installed() -> EngineDependencies | None:
    """The dependencies in force here, or None outside an application."""
    return _OVERRIDE.get() or _INSTALLED


def current() -> EngineDependencies:
    """The dependencies a handler must use, or an error explaining the gap."""
    dependencies = installed()
    if dependencies is None:
        raise DependenciesNotInstalledError(
            "no dataset perimeter is installed. Engine dependencies are resolved once "
            "by the application at startup (`geoparquet_mcp.app.create_app`, or the "
            "stdio entry point in `server.main`) and injected; a handler must not "
            "build its own. Call `dependencies.install(dependencies.resolve(config))` "
            "before serving."
        )
    return dependencies


@contextmanager
def using(dependencies: EngineDependencies) -> Iterator[EngineDependencies]:
    """Install dependencies for the duration of a block, then restore.

    The seam a test uses to pin a perimeter, and the one a per-request
    perimeter would use in production. Scoped to the current context, so it
    does not disturb anything running alongside it.
    """
    token = _OVERRIDE.set(dependencies)
    try:
        yield dependencies
    finally:
        _OVERRIDE.reset(token)


def engine_kwargs() -> dict[str, Any]:
    """The scope and session to pass to an engine operation. What handlers call."""
    return current().kwargs
