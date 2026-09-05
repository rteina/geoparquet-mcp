"""What the perimeter refuses, and how it refuses it.

`test_sources.py` already checks the shape of `DatasetScope`: names resolve,
narrowing never widens, a crafted S3 URL is rejected. This file is the other
half — the perimeter exercised over a real corpus on a real filesystem, where
the escapes are the ones an attacker would actually reach for.

The structure of the claim is worth stating, because it decides what is worth
testing. There are only two doors into a read:

  1. A dataset *name*, which every operation takes and which only the scope
     can turn into a path. Nothing here accepts a path, so an out-of-scope
     dataset has no route in at all — `fixture_elsewhere` below is registered
     and on disk, so being refused proves the scope rather than a missing file.
  2. A dataset *path*, which reaches exactly one caller: the benchmark, pinning
     one Parquet part. `assert_within` is the guard on that door, and the
     parametrised cases below are what a caller would try to walk through it.

Ad-hoc SQL looks like a third door and is not: table functions are the only
way to name a file in DuckDB SQL, and the parser refuses all of them. That is
checked in `test_engine_local.py` beside the rest of `run_sql`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import corpus
from geoparquet_mcp import engine
from geoparquet_mcp.engine.errors import ScopeViolationError, UnknownSourceError
from geoparquet_mcp.engine.sources import DatasetScope, Source

RELEASE = corpus.FIXTURE_RELEASE


@pytest.fixture
def places_only(registered_fixture_sources) -> DatasetScope:
    """A scope holding one of the three registered fixture datasets."""
    return DatasetScope.restricted_to([corpus.PLACES], release=RELEASE)


@pytest.fixture
def inside(places_only: DatasetScope) -> str:
    """A real part file that the scope does allow, as the control case."""
    return places_only.get(corpus.PLACES).prefix(RELEASE) + "part-00000.parquet"


# ---------------------------------------------------------------------------
# Door 1 — the name
# ---------------------------------------------------------------------------


def test_a_registered_dataset_left_out_of_the_scope_has_no_path(
    places_only: DatasetScope, registered_fixture_sources
) -> None:
    """The strongest form of the claim: the file exists, is readable, and is unreachable."""
    from geoparquet_mcp.engine import sources

    assert corpus.ELSEWHERE in sources.SOURCES
    assert Path(sources.SOURCES[corpus.ELSEWHERE].prefix(RELEASE) + "part-00000.parquet").is_file()

    with pytest.raises(UnknownSourceError) as caught:
        places_only.target(corpus.ELSEWHERE)
    assert corpus.PLACES in str(caught.value), "the refusal must say what is available"


@pytest.mark.parametrize(
    "operation",
    [
        lambda scope, session: engine.bbox_query(
            0, 0, 1, 1, source=corpus.ELSEWHERE, scope=scope, session=session
        ),
        lambda scope, session: engine.dataset_schema(
            corpus.ELSEWHERE, scope=scope, session=session
        ),
        lambda scope, session: engine.preview_rows(
            source=corpus.ELSEWHERE, scope=scope, session=session
        ),
        lambda scope, session: engine.nearest(
            lon=0.05, lat=0.05, source=corpus.ELSEWHERE, scope=scope, session=session
        ),
        lambda scope, session: engine.column_statistics(
            column="confidence", source=corpus.ELSEWHERE, scope=scope, session=session
        ),
        lambda scope, session: engine.attribute_aggregate(
            "confidence", source=corpus.ELSEWHERE, scope=scope, session=session
        ),
        lambda scope, session: engine.h3_aggregate(
            0, 0, 1, 1, source=corpus.ELSEWHERE, scope=scope, session=session
        ),
        lambda scope, session: engine.dataset_extent(
            corpus.ELSEWHERE, scope=scope, session=session
        ),
    ],
)
def test_no_operation_offers_a_way_round_the_name(places_only, local_session, operation) -> None:
    """Every entry point, not just the one someone remembered to guard."""
    with pytest.raises(UnknownSourceError):
        operation(places_only, local_session)


def test_no_operation_accepts_a_path_at_all() -> None:
    """The structural reason door 1 holds: there is no argument to put a path in.

    A guard that has to be called can be forgotten. This checks the stronger
    property — that a path is not expressible — by reading the signatures.
    """
    import inspect

    suspicious = {"path", "paths", "url", "file", "files", "target", "uri", "location"}
    offenders = []
    for name in (
        "list_datasets",
        "dataset_schema",
        "dataset_extent",
        "preview_rows",
        "spatial_filter",
        "bbox_query",
        "nearest",
        "attribute_aggregate",
        "column_statistics",
        "h3_aggregate",
        "point_in_polygon",
        "run_sql",
    ):
        parameters = set(inspect.signature(getattr(engine, name)).parameters)
        offenders.extend(f"{name}({p})" for p in parameters & suspicious)
    assert not offenders, (
        f"these operations take something that could carry a path: {offenders}. "
        "An operation must take a dataset name and ask the scope for the target."
    )


# ---------------------------------------------------------------------------
# Door 2 — the path, and everything a caller might try to smuggle through it
# ---------------------------------------------------------------------------


def test_the_control_case_is_allowed(places_only: DatasetScope, inside: str) -> None:
    """Without this the refusals below would also pass on a scope that refuses everything."""
    assert places_only.assert_within(inside) == inside


def test_a_sibling_dataset_under_the_same_root_is_refused(
    places_only: DatasetScope, registered_fixture_sources
) -> None:
    """Same corpus, same release, one directory across. The prefix is the boundary."""
    from geoparquet_mcp.engine import sources

    sibling = sources.SOURCES[corpus.ELSEWHERE].prefix(RELEASE) + "part-00000.parquet"
    with pytest.raises(ScopeViolationError, match="outside the scope"):
        places_only.assert_within(sibling)


def test_a_parent_directory_escape_is_refused(places_only: DatasetScope) -> None:
    """`..` is rejected on sight rather than resolved, so nothing has to be normalised."""
    prefix = places_only.get(corpus.PLACES).prefix(RELEASE)
    with pytest.raises(ScopeViolationError, match="only .parquet objects"):
        places_only.assert_within(f"{prefix}../../../../etc/passwd.parquet")


def test_an_escape_dressed_up_as_a_deeper_path_is_still_refused(
    places_only: DatasetScope,
) -> None:
    prefix = places_only.get(corpus.PLACES).prefix(RELEASE)
    with pytest.raises(ScopeViolationError):
        places_only.assert_within(f"{prefix}subdir/../../../secret.parquet")


def _scope_rooted_at(root: Path) -> DatasetScope:
    """A one-dataset scope over a throwaway directory.

    Built from a `Source` rather than looked up in the registry, so these tests
    depend on nothing another test installed, and so the shared corpus never
    grows a symlink inside a directory the other tests glob.
    """
    definition = Source(
        name=corpus.PLACES,
        title="link bait",
        description="",
        license="CC0-1.0",
        attribution="",
        theme="places",
        subtype="place",
        root=str(root),
    )
    return DatasetScope(release=RELEASE, sources={corpus.PLACES: definition})


def test_a_symlink_planted_inside_the_perimeter_cannot_reach_out_of_it(
    tmp_path: Path,
) -> None:
    """A filesystem has links; a key namespace does not. This is where they differ.

    The link's *name* is inside the perimeter and ends in `.parquet`, so a
    string comparison passes it and the engine would read whatever it points
    at. `assert_within` resolves a filesystem path before comparing, which is
    the only reason this is refused.
    """
    scope = _scope_rooted_at(tmp_path / "corpus")
    perimeter = Path(scope.get(corpus.PLACES).prefix(RELEASE))
    perimeter.mkdir(parents=True)

    outside = tmp_path / "outside" / "secrets.parquet"
    outside.parent.mkdir()
    outside.write_bytes(b"PAR1")

    link = perimeter / "innocent.parquet"
    link.symlink_to(outside)
    assert str(link).startswith(str(perimeter)), "the link's name is inside the perimeter"
    assert link.is_file(), "and it resolves to a readable file outside it"

    with pytest.raises(ScopeViolationError, match="outside the scope"):
        scope.assert_within(str(link))

    # A real file at the same place, on the other hand, is fine.
    genuine = perimeter / "part-00000.parquet"
    genuine.write_bytes(b"PAR1")
    assert scope.assert_within(str(genuine)) == str(genuine)


def test_a_symlinked_directory_inside_the_perimeter_is_refused_too(tmp_path: Path) -> None:
    """The same escape one level up: the link is the directory, not the file."""
    scope = _scope_rooted_at(tmp_path / "corpus")
    perimeter = Path(scope.get(corpus.PLACES).prefix(RELEASE))
    perimeter.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.parquet").write_bytes(b"PAR1")
    (perimeter / "extra").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ScopeViolationError, match="outside the scope"):
        scope.assert_within(str(perimeter / "extra" / "secrets.parquet"))


def test_a_symlinked_root_is_not_itself_an_escape(tmp_path: Path) -> None:
    """Resolving both sides, not just the candidate.

    Machines put temporary directories behind links — /tmp is one on macOS —
    so a guard that resolved only the candidate would refuse the corpus it was
    pointed at. That would be a bug that looks like security.
    """
    real_root = tmp_path / "real"
    linked_root = tmp_path / "linked"
    real_root.mkdir()
    linked_root.symlink_to(real_root, target_is_directory=True)

    scope = _scope_rooted_at(linked_root)
    perimeter = Path(scope.get(corpus.PLACES).prefix(RELEASE))
    perimeter.mkdir(parents=True)
    part = perimeter / "part-00000.parquet"
    part.write_bytes(b"PAR1")

    assert scope.assert_within(str(part)) == str(part)


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("/etc/passwd", "an absolute local path, no scheme, no HTTP"),
        ("/etc/passwd.parquet", "the same thing wearing the right extension"),
        ("file:///etc/shadow.parquet", "an absolute URL with a local scheme"),
        ("https://example.invalid/exfiltrate.parquet", "an absolute URL to another host"),
        ("s3://someone-elses-bucket/release/x/theme=places/type=place/p.parquet", "another bucket"),
        ("http://169.254.169.254/latest/meta-data.parquet", "the cloud metadata endpoint"),
        ("~/.ssh/id_rsa.parquet", "a shell-expanded home path, unexpanded here"),
        ("", "nothing at all"),
    ],
)
def test_an_absolute_path_or_url_is_refused_whatever_it_points_at(
    places_only: DatasetScope, path: str, why: str
) -> None:
    with pytest.raises(ScopeViolationError):
        places_only.assert_within(path)


def test_a_path_inside_the_perimeter_that_is_not_parquet_is_refused(
    places_only: DatasetScope,
) -> None:
    prefix = places_only.get(corpus.PLACES).prefix(RELEASE)
    for name in ("part-00000.csv", "part-00000.parquet.txt", "part-00000"):
        with pytest.raises(ScopeViolationError, match="only .parquet objects"):
            places_only.assert_within(prefix + name)


def test_the_release_is_part_of_the_perimeter(places_only: DatasetScope) -> None:
    """A different release is a different set of bytes, so it is outside."""
    other = places_only.get(corpus.PLACES).prefix("1999-01-01.0") + "part-00000.parquet"
    with pytest.raises(ScopeViolationError, match="outside the scope"):
        places_only.assert_within(other)


def test_a_prefix_that_merely_starts_the_same_is_refused(
    places_only: DatasetScope, tmp_path: Path
) -> None:
    """`/corpus/places-elsewhere/` starts with `/corpus/places` and is not under it."""
    prefix = places_only.get(corpus.PLACES).prefix(RELEASE).rstrip("/")
    with pytest.raises(ScopeViolationError, match="outside the scope"):
        places_only.assert_within(f"{prefix}-elsewhere/part-00000.parquet")


@pytest.mark.skipif(os.name == "nt", reason="POSIX path separators")
def test_a_redundant_separator_resolves_to_the_same_file(places_only: DatasetScope) -> None:
    prefix = places_only.get(corpus.PLACES).prefix(RELEASE)
    # Same file, spelled with a redundant separator: allowed, because it
    # resolves to the same place. The point is that it is *checked*, not that
    # unusual spelling is rejected.
    assert places_only.assert_within(f"{prefix}/part-00000.parquet")


# ---------------------------------------------------------------------------
# The perimeter cannot be widened from inside
# ---------------------------------------------------------------------------


def test_narrowing_is_the_only_direction(places_only: DatasetScope, local_scope) -> None:
    assert local_scope.narrowed_to([corpus.PLACES]).names == [corpus.PLACES]
    with pytest.raises(ScopeViolationError, match="cannot widen"):
        places_only.narrowed_to([corpus.PLACES, corpus.DIVISIONS])


def test_a_scope_cannot_be_built_over_something_that_was_never_registered() -> None:
    with pytest.raises(UnknownSourceError):
        DatasetScope.restricted_to(["fixture_places", "not_registered"], release=RELEASE)


def test_the_scopes_mapping_cannot_be_mutated(places_only: DatasetScope) -> None:
    """Frozen dataclass, read-only mapping: no route to adding a source after the fact."""
    with pytest.raises(TypeError):
        places_only.sources["smuggled"] = places_only.get(corpus.PLACES)
