"""The closed set of remote GeoParquet datasets the engine may read.

A source is a name, a URL pattern, and enough metadata for a caller to decide
whether it is the right dataset. Nothing here downloads anything: resolving a
source produces a string that goes straight into DuckDB's `read_parquet()`.

Why a scope
-----------
The registry below is already a closed perimeter in practice. `DatasetScope`
makes it one by construction: it is the only thing in the engine that turns a
name into a path, and no operation accepts a path. An engine built with a
restricted scope cannot be talked into reading a dataset outside it, because
there is no argument through which a path could arrive — the same isolation a
multi-tenant service gets from resolving the tenant once, at the boundary,
and never again from user input.

About the Overture release path
-------------------------------
Overture Maps Foundation publishes a new release roughly every month and
keeps only the last two on the public bucket (the objects carry a 60-day
retention rule). A hard-coded release path therefore expires. The pinned
release below is the one verified working on the date noted; when it is gone,
`resolve_release()` lists the bucket and picks the newest release instead of
failing.
"""

from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

from geoparquet_mcp.engine.errors import ScopeViolationError, UnknownSourceError

# Overture's public bucket. Anonymous reads, no credentials, no sign-up.
OVERTURE_BUCKET = "overturemaps-us-west-2"
OVERTURE_REGION = "us-west-2"
OVERTURE_HTTPS_ENDPOINT = f"https://{OVERTURE_BUCKET}.s3.{OVERTURE_REGION}.amazonaws.com"

# Where releases live. A source's `root` defaults to this; a test corpus sets
# it to a local directory so the same operations run against the same layout
# with no network. Nothing else about a source changes.
OVERTURE_ROOT = f"s3://{OVERTURE_BUCKET}/release"

# Verified reachable and queryable on 2026-09-05 with DuckDB 1.5.5 + httpfs.
# Overture retains only the two most recent releases, so treat this as a
# default rather than a guarantee: resolve_release() falls back to discovery.
OVERTURE_PINNED_RELEASE = "2026-08-19.0"

_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# `scheme://` at the head of a path. Used to tell an object-storage URL, where
# a string prefix is the whole truth, from a filesystem path, where it is not.
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _within_prefix(path: str, prefix: str) -> bool:
    """True when `path` really lies under `prefix`.

    For an object-storage URL the string comparison is the whole truth: a key
    namespace is flat and has no links, so a key that starts with the prefix
    is under it, full stop.

    A filesystem path is different, and the difference is the point. A symlink
    planted inside the perimeter has a name that starts with the prefix and a
    target that does not, so comparing strings would let it through and the
    engine would read whatever it pointed at. Both sides are resolved before
    comparing, which also settles the mundane case of the root itself sitting
    behind a link — /tmp is one on macOS.
    """
    if not path.startswith(prefix):
        return False
    if _URL_SCHEME.match(prefix):
        return True
    try:
        resolved = Path(path).resolve()
        root = Path(prefix).resolve()
    except OSError:  # pragma: no cover - a path the OS refuses to resolve
        return False
    return root in resolved.parents


@dataclass(frozen=True)
class Source:
    """A remote GeoParquet dataset the engine knows how to query."""

    name: str
    title: str
    description: str
    license: str
    attribution: str
    theme: str
    subtype: str
    # Column holding the precomputed bounding box. Overture stores
    # STRUCT(xmin, xmax, ymin, ymax), whose per-row-group Parquet statistics
    # are what makes a spatial filter prunable without any index.
    bbox_column: str = "bbox"
    geometry_column: str = "geometry"
    # Fields worth returning by default; keeping the projection narrow is half
    # of the byte saving.
    default_columns: tuple[str, ...] = ()
    # Column carrying a human-readable label, when the dataset has one.
    name_column: str | None = "names.primary"
    # Column carrying a classification, when the dataset has one.
    category_column: str | None = None
    # Column carrying a per-record confidence in 0..1, when the dataset has one.
    confidence_column: str | None = None
    # True when the dataset holds areal geometry, so it can be the polygon
    # side of a point-in-polygon join.
    polygonal: bool = False
    # Rough scale, for a caller choosing between sources.
    approximate_rows: int | None = None
    approximate_bytes: int | None = None
    notes: str = ""
    # The storage root the releases sit under. Overture's public bucket by
    # default; a local directory for a test corpus laid out the same way.
    root: str = OVERTURE_ROOT

    def prefix(self, release: str) -> str:
        """The object-storage folder holding this source's Parquet parts."""
        return f"{self.root}/{release}/theme={self.theme}/type={self.subtype}/"

    def scan_target(self, release: str) -> str:
        """The `read_parquet()` argument for this source at a given release."""
        return f"{self.prefix(release)}*.parquet"

    def https_prefix(self, release: str) -> str:
        """The same location as a plain HTTPS URL, for humans and curl.

        Only meaningful for a source on the public bucket; anything else is
        already a path a human can open, so it is returned as it is.
        """
        if self.root != OVERTURE_ROOT:
            return self.prefix(release)
        return (
            f"{OVERTURE_HTTPS_ENDPOINT}/release/{release}/theme={self.theme}/type={self.subtype}/"
        )


SOURCES: dict[str, Source] = {
    "overture_places": Source(
        name="overture_places",
        title="Overture Maps — places",
        description=(
            "Points of interest worldwide: name, category, confidence, address "
            "and source attribution."
        ),
        license="CDLA-Permissive-2.0 (data); ODbL applies to OpenStreetMap-derived records",
        attribution="© Overture Maps Foundation",
        theme="places",
        subtype="place",
        category_column="categories.primary",
        confidence_column="confidence",
        default_columns=(
            "id",
            "names.primary AS name",
            "categories.primary AS category",
            "confidence",
            "addresses[1].freeform AS address",
            "addresses[1].locality AS locality",
            "addresses[1].country AS country",
            "bbox.xmin AS longitude",
            "bbox.ymin AS latitude",
        ),
        approximate_rows=73_631_092,
        approximate_bytes=10_480_684_059,
        notes=(
            "16 Parquet parts, ~10.5 GB. The default source: a city-scale query answers in seconds."
        ),
    ),
    "overture_divisions": Source(
        name="overture_divisions",
        title="Overture Maps — division areas",
        description=(
            "Administrative area polygons worldwide, from country down to "
            "locality, with admin level and class."
        ),
        license="ODbL / CDLA-Permissive-2.0 depending on the contributing source",
        attribution="© Overture Maps Foundation",
        theme="divisions",
        subtype="division_area",
        category_column="subtype",
        polygonal=True,
        default_columns=(
            "id",
            "names.primary AS name",
            "subtype",
            "class",
            "country",
            "region",
        ),
        approximate_rows=1_074_177,
        approximate_bytes=4_474_605_608,
        notes=(
            "8 Parquet parts, ~4.5 GB. The polygon side of point_in_polygon: small enough "
            "that a city-scale containment join stays interactive."
        ),
    ),
    "overture_buildings": Source(
        name="overture_buildings",
        title="Overture Maps — buildings",
        description="Building footprints worldwide, with height and class where known.",
        license="ODbL / CDLA-Permissive-2.0 depending on the contributing source",
        attribution="© Overture Maps Foundation",
        theme="buildings",
        subtype="building",
        category_column="class",
        polygonal=True,
        default_columns=(
            "id",
            "names.primary AS name",
            "class",
            "height",
            "num_floors",
            "bbox.xmin AS longitude",
            "bbox.ymin AS latitude",
        ),
        approximate_rows=None,
        approximate_bytes=277_419_729_506,
        notes=(
            "513 Parquet parts, ~277 GB. Pushdown still cuts a city query to well under "
            "a gigabyte, but reading 513 file footers makes it a minute-scale query. "
            "Prefer overture_places for interactive work."
        ),
    ),
}

DEFAULT_SOURCE = "overture_places"


def list_releases(timeout: int = 15) -> list[str]:
    """List the Overture releases currently present on the public bucket."""
    url = f"{OVERTURE_HTTPS_ENDPOINT}/?list-type=2&prefix=release/&delimiter=/"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        root = ElementTree.fromstring(response.read())
    releases = []
    for prefix in root.findall("s3:CommonPrefixes/s3:Prefix", _S3_NS):
        text = (prefix.text or "").removeprefix("release/").rstrip("/")
        if text and text[0].isdigit():
            releases.append(text)
    return sorted(releases)


@lru_cache(maxsize=1)
def resolve_release(pinned: str = OVERTURE_PINNED_RELEASE) -> str:
    """Return the release to query.

    The pinned release is used when it is still on the bucket. Once Overture
    expires it, the newest available release is used instead, so the demo
    keeps working without a code change. Discovery failures fall back to the
    pin rather than raising, because a stale pin still produces a clearer
    error later than a network error here.
    """
    try:
        releases = list_releases()
    except Exception:  # noqa: BLE001 - network discovery is best-effort
        return pinned
    if pinned in releases:
        return pinned
    return releases[-1] if releases else pinned


@dataclass(frozen=True)
class DatasetScope:
    """The datasets an engine is allowed to read, resolved once.

    Contract: a scope is the only way to obtain a path the engine will read.
    Operations take a source *name* and ask the scope for the target, so a
    caller has no argument through which an arbitrary URL could arrive. When
    a path must be handled anyway — the benchmark pins one Parquet part to
    keep its unpushed comparison affordable — `assert_within()` checks it
    against the scope's own prefixes and raises rather than reading.

    A scope is immutable and cheap to copy; narrowing one with `narrowed_to()`
    can only ever remove datasets.
    """

    release: str
    sources: Mapping[str, Source]

    @classmethod
    def default(cls, release: str | None = None) -> DatasetScope:
        """Every registered source, at the currently resolvable release."""
        return cls(release=release or resolve_release(), sources=MappingProxyType(dict(SOURCES)))

    @classmethod
    def restricted_to(cls, names: Iterable[str], release: str | None = None) -> DatasetScope:
        """A scope holding only the named sources.

        Unknown names are rejected here, at construction, rather than at the
        first query.
        """
        wanted = list(names)
        unknown = [name for name in wanted if name not in SOURCES]
        if unknown:
            raise UnknownSourceError(
                f"cannot build a scope over unregistered source(s) {', '.join(sorted(unknown))}; "
                f"registered sources: {', '.join(sorted(SOURCES))}"
            )
        return cls(
            release=release or resolve_release(),
            sources=MappingProxyType({name: SOURCES[name] for name in wanted}),
        )

    def narrowed_to(self, names: Iterable[str]) -> DatasetScope:
        """A sub-scope of this one. Never widens: names outside are refused."""
        wanted = list(names)
        outside = [name for name in wanted if name not in self.sources]
        if outside:
            raise ScopeViolationError(
                f"cannot widen a scope: {', '.join(sorted(outside))} "
                f"is not in the current scope ({', '.join(self.names)})"
            )
        return DatasetScope(
            release=self.release,
            sources=MappingProxyType({name: self.sources[name] for name in wanted}),
        )

    @property
    def names(self) -> list[str]:
        """The source names in this scope, sorted."""
        return sorted(self.sources)

    def get(self, name: str) -> Source:
        """Resolve a source name inside this scope, or explain what is available."""
        try:
            return self.sources[name]
        except KeyError as exc:
            raise UnknownSourceError(
                f"unknown source {name!r}; sources available in this scope: {', '.join(self.names)}"
            ) from exc

    def target(self, name: str) -> str:
        """The `read_parquet()` target for a source in this scope.

        The single place a readable path is produced.
        """
        return self.get(name).scan_target(self.release)

    def assert_within(self, path: str) -> str:
        """Return `path` if it lies under one of this scope's prefixes, else raise.

        The guard for the one code path that handles a path rather than a
        name. It compares against prefixes this scope built itself, so a
        crafted URL — another bucket, another release, a parent-directory
        escape — cannot pass, and a filesystem path is resolved first so a
        symlink pointing out of the perimeter cannot either.
        """
        if ".." in path or not path.endswith(".parquet"):
            raise ScopeViolationError(
                f"refusing to read {path!r}: only .parquet objects inside the scope are readable"
            )
        for source in self.sources.values():
            if _within_prefix(path, source.prefix(self.release)):
                return path
        raise ScopeViolationError(
            f"refusing to read {path!r}: outside the scope "
            f"({', '.join(self.names)} at release {self.release})"
        )

    def entries(self) -> list[dict[str, object]]:
        """The scope's catalogue, as plain data."""
        return [
            {
                "name": source.name,
                "title": source.title,
                "description": source.description,
                "license": source.license,
                "attribution": source.attribution,
                "release": self.release,
                "scan_target": source.scan_target(self.release),
                "https_prefix": source.https_prefix(self.release),
                "bbox_column": source.bbox_column,
                "geometry_column": source.geometry_column,
                "name_column": source.name_column,
                "category_column": source.category_column,
                "polygonal": source.polygonal,
                "default_columns": list(source.default_columns),
                "approximate_rows": source.approximate_rows,
                "approximate_bytes": source.approximate_bytes,
                "notes": source.notes,
            }
            for source in self.sources.values()
        ]


@lru_cache(maxsize=1)
def default_scope() -> DatasetScope:
    """The scope the engine uses when a caller does not supply one.

    Cached because building it resolves the release, which touches the
    network. Call `resolve_release.cache_clear()` and this one's to force a
    re-resolution after a release expires mid-process.
    """
    return DatasetScope.default()


def get_source(name: str) -> Source:
    """Resolve a source name against the default scope."""
    return default_scope().get(name)


def scan_target(source_name: str = DEFAULT_SOURCE) -> str:
    """Resolve a source name to a DuckDB `read_parquet()` target."""
    return default_scope().target(source_name)


def describe_sources() -> list[dict[str, object]]:
    """The default scope's catalogue, as plain data."""
    return default_scope().entries()
