"""The source catalogue and dataset schemas, exposed as MCP resources.

Tools answer questions; resources describe what there is to ask about. A
client pulls these into context once instead of spending a tool call to learn
what exists — which is why `list_sources` is not a tool: the catalogue is a
document, and MCP already has a primitive for a document.

Both views read the perimeter the application injected, not the process-wide
default. That matters more here than it looks: a resource that listed every
registered dataset while the tools could only query three would be advertising
a capability the server does not have, and an agent would waste its next call
finding out.
"""

from __future__ import annotations

import json
from typing import Any

from geoparquet_mcp import dependencies, engine

CATALOG_URI = "geoparquet://sources"
SOURCE_URI_TEMPLATE = "geoparquet://sources/{source}"
SCHEMA_URI_TEMPLATE = "geoparquet://sources/{source}/schema"


def _document(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)


def catalog_document() -> str:
    """Every dataset in scope, with licence, release path and rough size."""
    scope = dependencies.current().scope
    return _document(
        {
            "release": scope.release,
            "default_source": engine.DEFAULT_SOURCE,
            "sources": scope.entries(),
        }
    )


def source_document(source: str) -> str:
    """One catalogue entry: what the dataset is, where it lives, what it costs."""
    scope = dependencies.current().scope
    entries = [entry for entry in scope.entries() if entry["name"] == source]
    if not entries:
        # Routed through the engine's error so an unknown name reads the same
        # here as it does from a tool, and names the sources actually in scope.
        scope.get(source)
    return _document(entries[0])


def schema_document(source: str) -> str:
    """One dataset's columns, types, CRS and extent, read from Parquet footers.

    The resource form of `geoparquet_describe_source`: same answer, no tool
    call. Reading it costs footer metadata only — kilobytes against a
    multi-gigabyte dataset — which is what makes it reasonable as something a
    client fetches up front.
    """
    return _document(engine.dataset_schema(source=source, **dependencies.engine_kwargs()))


def register(server) -> None:
    server.resource(
        CATALOG_URI,
        name="source_catalog",
        description=(
            "Every remote GeoParquet dataset this server can query, with licence, "
            "current release path, geometry and category columns, and approximate "
            "size. Read this first: it is the list of names every tool accepts."
        ),
        mime_type="application/json",
    )(catalog_document)
    server.resource(
        SOURCE_URI_TEMPLATE,
        name="source_entry",
        description="Catalogue entry for a single remote GeoParquet source.",
        mime_type="application/json",
    )(source_document)
    server.resource(
        SCHEMA_URI_TEMPLATE,
        name="source_schema",
        description=(
            "Column schema, types, coordinate reference system, row count and "
            "geographic extent of one dataset, read from Parquet footer metadata "
            "without scanning any data. The read-only form of "
            "geoparquet_describe_source."
        ),
        mime_type="application/json",
    )(schema_document)
