"""The source registry and release resolution."""

from __future__ import annotations

import pytest

from geoparquet_mcp import sources


def test_default_source_is_registered() -> None:
    assert sources.DEFAULT_SOURCE in sources.SOURCES


def test_unknown_source_names_the_known_ones() -> None:
    with pytest.raises(sources.UnknownSourceError) as excinfo:
        sources.get_source("no_such_source")
    assert "overture_places" in str(excinfo.value)


def test_scan_target_is_a_partitioned_glob() -> None:
    target = sources.get_source("overture_places").scan_target("2026-08-19.0")
    assert target == (
        "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/*.parquet"
    )


def test_https_prefix_points_at_the_same_objects() -> None:
    source = sources.get_source("overture_places")
    prefix = source.https_prefix("2026-08-19.0")
    assert prefix.startswith("https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/")
    assert prefix.endswith("/theme=places/type=place/")


def test_every_source_declares_a_licence_and_attribution() -> None:
    for source in sources.SOURCES.values():
        assert source.license
        assert source.attribution


@pytest.mark.network
def test_release_resolution_returns_a_release_on_the_bucket() -> None:
    releases = sources.list_releases()
    assert releases, "the public bucket listed no releases"
    assert sources.resolve_release() in releases
