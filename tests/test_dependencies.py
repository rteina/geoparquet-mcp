"""The dependency adapter: the perimeter is resolved once and cannot be widened.

These tests are about an architectural property rather than a behaviour. The
property is: a handler cannot read a dataset the application did not put in
scope. It is worth testing because it is the kind of thing that stays true
right up until someone adds a convenient default.
"""

from __future__ import annotations

import pytest

from geoparquet_mcp import dependencies
from geoparquet_mcp.config import AppConfig
from geoparquet_mcp.dependencies import (
    DependenciesNotInstalledError,
    EngineDependencies,
)
from geoparquet_mcp.engine import DatasetScope
from geoparquet_mcp.engine.errors import ScopeViolationError, UnknownSourceError
from geoparquet_mcp.engine.session import Session, SessionConfig

RELEASE = "2026-08-19.0"


@pytest.fixture
def session() -> Session:
    """A DuckDB session with no spatial extension: these tests never query."""
    return Session(SessionConfig(extensions=("httpfs",)))


@pytest.fixture
def narrow(session: Session) -> EngineDependencies:
    scope = DatasetScope.restricted_to(["overture_places"], release=RELEASE)
    return EngineDependencies(scope=scope, session=session)


def test_a_handler_outside_an_application_gets_an_explanation_not_a_default() -> None:
    """The absence of a perimeter must be loud.

    A silent fallback to the default scope is the exact bug this module
    exists to prevent: a deployment restricted to one dataset would quietly
    serve all three.
    """
    with pytest.raises(DependenciesNotInstalledError) as caught:
        dependencies.current()
    assert "resolved once" in str(caught.value)


def test_installed_dependencies_are_what_a_handler_sees(narrow: EngineDependencies) -> None:
    with dependencies.using(narrow):
        assert dependencies.current() is narrow
        assert dependencies.engine_kwargs() == {"scope": narrow.scope, "session": narrow.session}


def test_the_perimeter_is_restored_after_the_block(narrow: EngineDependencies) -> None:
    with dependencies.using(narrow):
        pass
    assert dependencies.installed() is None


def test_a_handler_can_narrow_the_perimeter(narrow: EngineDependencies) -> None:
    narrower = narrow.narrowed_to(["overture_places"])
    assert narrower.scope.names == ["overture_places"]
    assert narrower.session is narrow.session


def test_a_handler_cannot_widen_the_perimeter(narrow: EngineDependencies) -> None:
    """The whole point. Widening is refused by the scope, not by a check here."""
    with pytest.raises(ScopeViolationError):
        narrow.narrowed_to(["overture_places", "overture_buildings"])


def test_a_dataset_outside_the_perimeter_has_no_path(narrow: EngineDependencies) -> None:
    """Refused at name resolution, so no operation can be handed a target for it."""
    with pytest.raises(UnknownSourceError):
        narrow.scope.target("overture_buildings")


def test_resolve_honours_a_restricted_configuration() -> None:
    resolved = dependencies.resolve(
        AppConfig(sources=("overture_places", "overture_divisions"), release=RELEASE)
    )
    assert resolved.scope.names == ["overture_divisions", "overture_places"]
    assert resolved.scope.release == RELEASE


def test_resolve_rejects_an_unregistered_dataset_at_startup() -> None:
    """A typo in the environment fails the process, not the first request."""
    with pytest.raises(UnknownSourceError):
        dependencies.resolve(AppConfig(sources=("overture_moon",), release=RELEASE))


def test_the_adapter_does_not_know_which_protocol_called_it() -> None:
    """It bridges handlers to the engine; knowing both sides would be coupling.

    Checked as an import fact rather than an intention: `dependencies.py`
    imports neither the protocol library nor the web framework.
    """
    import ast
    import pathlib

    source = pathlib.Path(dependencies.__file__).read_text(encoding="utf-8")
    roots = {
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
    }
    assert "mcp" not in roots
    assert "fastapi" not in roots
