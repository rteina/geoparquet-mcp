"""Shared fixtures: the local corpus, and a perimeter over it.

The corpus is built once per test session into a temporary directory and the
resulting sources are registered in `SOURCES` for the duration. Registering
them is what lets the *production* path be the one under test: `AppConfig` →
`dependencies.resolve` → `DatasetScope.restricted_to` → the operations. A
test that hand-built a scope and passed it around would exercise less.

Nothing here resolves a release, so nothing here touches the network. The
`network` marker stays what it means: reads the remote dataset.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import corpus
from geoparquet_mcp.config import AppConfig
from geoparquet_mcp.dependencies import EngineDependencies
from geoparquet_mcp.engine import sources
from geoparquet_mcp.engine.session import Session, SessionConfig
from geoparquet_mcp.engine.sources import DatasetScope


@pytest.fixture(scope="session")
def corpus_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The generated Parquet corpus, written once for the whole session."""
    return corpus.build(tmp_path_factory.mktemp("corpus"))


@pytest.fixture(scope="session")
def registered_fixture_sources(corpus_root: Path) -> Iterator[dict[str, sources.Source]]:
    """Put the corpus in the source registry, and take it out again.

    Session-scoped and mutating a module global, which is worth a word: the
    registry is how a deployment's configuration names a dataset, so a fixture
    dataset has to be in it for `AppConfig(sources=...)` to reach the corpus.
    The alternative — injecting a scope past the configuration — would leave
    the resolution path untested, which is the path a misconfiguration breaks.
    """
    entries = corpus.sources_for(corpus_root)
    sources.SOURCES.update(entries)
    try:
        yield entries
    finally:
        for name in entries:
            sources.SOURCES.pop(name, None)


@pytest.fixture(scope="session")
def local_session() -> Iterator[Session]:
    """A DuckDB session with `spatial` and no `httpfs`.

    Deliberately unable to speak HTTP: if an operation under test ever
    resolved a remote path it would fail here rather than quietly reading the
    bucket and passing.
    """
    session = Session(SessionConfig(extensions=("spatial",)))
    yield session
    session.close()


@pytest.fixture
def local_scope(registered_fixture_sources: dict[str, sources.Source]) -> DatasetScope:
    """Places and divisions. `fixture_elsewhere` is registered but left out."""
    return DatasetScope.restricted_to(
        [corpus.PLACES, corpus.DIVISIONS], release=corpus.FIXTURE_RELEASE
    )


@pytest.fixture
def engine_kwargs(local_scope: DatasetScope, local_session: Session) -> dict[str, object]:
    """The two arguments every engine operation takes, ready to splat."""
    return {"scope": local_scope, "session": local_session}


@pytest.fixture
def local_dependencies(local_scope: DatasetScope, local_session: Session) -> EngineDependencies:
    """The perimeter a handler would be handed, over the corpus."""
    return EngineDependencies(scope=local_scope, session=local_session)


@pytest.fixture
def local_config(registered_fixture_sources: dict[str, sources.Source]) -> AppConfig:
    """An application configuration pinned to the corpus.

    `testserver` is named because `TestClient` sends it as the Host header and
    the MCP transport's DNS-rebinding protection would otherwise answer 421 —
    the same thing a deployment behind a proxy has to declare.
    """
    return AppConfig(
        mcp_enabled=True,
        sources=(corpus.PLACES, corpus.DIVISIONS),
        release=corpus.FIXTURE_RELEASE,
        allowed_hosts=("testserver",),
    )
