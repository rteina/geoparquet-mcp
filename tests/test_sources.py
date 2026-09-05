"""The source registry, release resolution, and the dataset scope."""

from __future__ import annotations

import pytest

from geoparquet_mcp.engine import sources
from geoparquet_mcp.engine.errors import ScopeViolationError, UnknownSourceError
from geoparquet_mcp.engine.sources import DatasetScope

RELEASE = "2026-08-19.0"


def test_default_source_is_registered() -> None:
    assert sources.DEFAULT_SOURCE in sources.SOURCES


def test_scan_target_is_a_partitioned_glob() -> None:
    target = sources.SOURCES["overture_places"].scan_target(RELEASE)
    assert target == (
        "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/*.parquet"
    )


def test_https_prefix_points_at_the_same_objects() -> None:
    source = sources.SOURCES["overture_places"]
    prefix = source.https_prefix(RELEASE)
    assert prefix.startswith("https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/")
    assert prefix.endswith("/theme=places/type=place/")


def test_every_source_declares_a_licence_and_attribution() -> None:
    for source in sources.SOURCES.values():
        assert source.license
        assert source.attribution


# ---------------------------------------------------------------------------
# DatasetScope — the perimeter
# ---------------------------------------------------------------------------


def _scope(*names: str) -> DatasetScope:
    return DatasetScope.restricted_to(names or ("overture_places",), release=RELEASE)


def test_a_scope_resolves_only_the_sources_it_holds() -> None:
    scope = _scope("overture_places")
    assert scope.names == ["overture_places"]
    assert scope.target("overture_places").endswith("/theme=places/type=place/*.parquet")


def test_a_source_outside_the_scope_is_refused_even_though_it_is_registered() -> None:
    scope = _scope("overture_places")
    assert "overture_buildings" in sources.SOURCES
    with pytest.raises(UnknownSourceError) as excinfo:
        scope.get("overture_buildings")
    # The message must tell a caller what it *can* ask for.
    assert "overture_places" in str(excinfo.value)


def test_unknown_source_names_the_known_ones() -> None:
    with pytest.raises(UnknownSourceError) as excinfo:
        sources.default_scope().get("no_such_source")
    assert "overture_places" in str(excinfo.value)


def test_a_scope_cannot_be_built_over_an_unregistered_source() -> None:
    with pytest.raises(UnknownSourceError):
        DatasetScope.restricted_to(["overture_places", "nope"], release=RELEASE)


def test_narrowing_removes_sources_and_never_adds_them() -> None:
    scope = _scope("overture_places", "overture_divisions")
    assert scope.narrowed_to(["overture_places"]).names == ["overture_places"]
    with pytest.raises(ScopeViolationError):
        scope.narrowed_to(["overture_buildings"])


@pytest.mark.parametrize(
    "path",
    [
        # Another bucket entirely.
        "s3://someone-elses-bucket/release/2026-08-19.0/theme=places/type=place/part-0.parquet",
        # Right bucket, wrong theme — a dataset outside the scope.
        "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=buildings/type=building/p.parquet",
        # Right prefix, wrong release.
        "s3://overturemaps-us-west-2/release/1999-01-01.0/theme=places/type=place/p.parquet",
        # Parent-directory escape.
        "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/../../p.parquet",
        # Not a Parquet object.
        "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/secrets.csv",
        # A local file, which is the interesting one: no HTTP involved at all.
        "/etc/passwd",
    ],
)
def test_paths_outside_the_scope_are_refused(path: str) -> None:
    with pytest.raises(ScopeViolationError):
        _scope("overture_places").assert_within(path)


def test_a_part_file_inside_the_scope_is_allowed() -> None:
    scope = _scope("overture_places")
    part = (
        "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/"
        "part-00000-abc.zstd.parquet"
    )
    assert scope.assert_within(part) == part


def test_the_catalogue_carries_licence_and_release_for_every_entry() -> None:
    for entry in _scope("overture_places", "overture_divisions").entries():
        assert entry["release"] == RELEASE
        assert entry["license"]
        assert entry["scan_target"].startswith("s3://")


@pytest.mark.network
def test_release_resolution_returns_a_release_on_the_bucket() -> None:
    releases = sources.list_releases()
    assert releases, "the public bucket listed no releases"
    assert sources.resolve_release() in releases
