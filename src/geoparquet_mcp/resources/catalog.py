"""The source catalogue, exposed as MCP resources.

Tools answer questions; resources describe what there is to ask about. Both
views are backed by the same registry in `sources.py`, so a client can pull
the catalogue into context once instead of calling a tool for it.
"""

from __future__ import annotations

import json

from geoparquet_mcp.engine import sources

CATALOG_URI = "geoparquet://sources"
SOURCE_URI_TEMPLATE = "geoparquet://sources/{source}"


def catalog_document() -> str:
    """The whole catalogue as a JSON document."""
    return json.dumps(
        {
            "release": sources.resolve_release(),
            "default_source": sources.DEFAULT_SOURCE,
            "sources": sources.describe_sources(),
        },
        indent=2,
    )


def source_document(source: str) -> str:
    """One catalogue entry as a JSON document."""
    definition = sources.get_source(source)
    release = sources.resolve_release()
    return json.dumps(
        {
            "name": definition.name,
            "title": definition.title,
            "description": definition.description,
            "license": definition.license,
            "attribution": definition.attribution,
            "release": release,
            "scan_target": definition.scan_target(release),
            "https_prefix": definition.https_prefix(release),
            "bbox_column": definition.bbox_column,
            "geometry_column": definition.geometry_column,
            "default_columns": list(definition.default_columns),
            "approximate_rows": definition.approximate_rows,
            "approximate_bytes": definition.approximate_bytes,
            "notes": definition.notes,
        },
        indent=2,
    )


def register(server) -> None:
    server.resource(
        CATALOG_URI,
        name="source_catalog",
        description="Every remote GeoParquet dataset this server can query.",
        mime_type="application/json",
    )(catalog_document)
    server.resource(
        SOURCE_URI_TEMPLATE,
        name="source_entry",
        description="Catalogue entry for a single remote GeoParquet source.",
        mime_type="application/json",
    )(source_document)
