"""Resolution of the remote GeoParquet files this server can query.

A source is a name, a URL pattern, and enough metadata for an agent to decide
whether it is the right dataset. Nothing here downloads anything: resolving a
source produces a string that goes straight into DuckDB's `read_parquet()`.

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

import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from functools import lru_cache

# Overture's public bucket. Anonymous reads, no credentials, no sign-up.
OVERTURE_BUCKET = "overturemaps-us-west-2"
OVERTURE_REGION = "us-west-2"
OVERTURE_HTTPS_ENDPOINT = f"https://{OVERTURE_BUCKET}.s3.{OVERTURE_REGION}.amazonaws.com"

# Verified reachable and queryable on 2026-09-05 with DuckDB 1.5.5 + httpfs.
# Overture retains only the two most recent releases, so treat this as a
# default rather than a guarantee: resolve_release() falls back to discovery.
OVERTURE_PINNED_RELEASE = "2026-08-19.0"

_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


@dataclass(frozen=True)
class Source:
    """A remote GeoParquet dataset the server knows how to query."""

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
    # Rough scale, for an agent choosing between sources.
    approximate_rows: int | None = None
    approximate_bytes: int | None = None
    notes: str = ""

    def scan_target(self, release: str) -> str:
        """The `read_parquet()` argument for this source at a given release."""
        return (
            f"s3://{OVERTURE_BUCKET}/release/{release}"
            f"/theme={self.theme}/type={self.subtype}/*.parquet"
        )

    def https_prefix(self, release: str) -> str:
        """The same location as a plain HTTPS URL, for humans and curl."""
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
    "overture_buildings": Source(
        name="overture_buildings",
        title="Overture Maps — buildings",
        description="Building footprints worldwide, with height and class where known.",
        license="ODbL / CDLA-Permissive-2.0 depending on the contributing source",
        attribution="© Overture Maps Foundation",
        theme="buildings",
        subtype="building",
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


class UnknownSourceError(KeyError):
    """Raised when a tool is handed a source name that is not registered."""


def get_source(name: str) -> Source:
    try:
        return SOURCES[name]
    except KeyError as exc:
        known = ", ".join(sorted(SOURCES))
        raise UnknownSourceError(f"unknown source {name!r}; known sources: {known}") from exc


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


def scan_target(source_name: str = DEFAULT_SOURCE) -> str:
    """Resolve a source name to a DuckDB `read_parquet()` target."""
    return get_source(source_name).scan_target(resolve_release())


def describe_sources() -> list[dict[str, object]]:
    """The catalogue, as plain data."""
    release = resolve_release()
    return [
        {
            "name": source.name,
            "title": source.title,
            "description": source.description,
            "license": source.license,
            "attribution": source.attribution,
            "release": release,
            "scan_target": source.scan_target(release),
            "https_prefix": source.https_prefix(release),
            "bbox_column": source.bbox_column,
            "geometry_column": source.geometry_column,
            "approximate_rows": source.approximate_rows,
            "approximate_bytes": source.approximate_bytes,
            "notes": source.notes,
        }
        for source in SOURCES.values()
    ]
