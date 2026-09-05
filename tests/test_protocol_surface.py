"""The agent-facing surface, frozen.

`test_server.py` checks the *rules* the surface obeys: eight tools, no more;
every description carries the three headings a model needs; the catalogue is a
resource rather than a tool. This file checks the surface itself — every name,
every description, every input schema, byte for byte, against a committed
file.

That is a deliberately annoying test, and the annoyance is the feature. The
tool list and its descriptions are the entire contract with a language model:
renaming a parameter, tightening an enum, or rewriting a sentence about when
to reach for a tool changes the behaviour of every agent using this server,
and none of it shows up in a diff of the engine. A snapshot turns "I edited a
docstring" into a review of the interface.

Regenerating it is one command, and it is meant to be run deliberately:

    UPDATE_SNAPSHOTS=1 pytest tests/test_protocol_surface.py

then read the diff before committing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from geoparquet_mcp.server import build_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "mcp_surface.json"


def _dump(model: Any) -> dict[str, Any]:
    """One protocol object as plain JSON, with unset fields dropped.

    `exclude_none` keeps the snapshot about what the server actually declares
    rather than about which optional fields the SDK happens to define this
    week — a new optional field on the SDK's `Tool` model is not a change to
    this server's surface, and should not fail this test.
    """
    return model.model_dump(mode="json", exclude_none=True)


async def _surface() -> dict[str, Any]:
    server = build_server()
    return {
        "server": {
            "name": server.name,
            "version": server.version,
            "instructions": server.instructions,
        },
        "tools": sorted((_dump(tool) for tool in await server.list_tools()), key=_by_name),
        "resources": sorted(
            (_dump(resource) for resource in await server.list_resources()), key=_by_name
        ),
        "resource_templates": sorted(
            (_dump(template) for template in await server.list_resource_templates()),
            key=_by_name,
        ),
    }


def _by_name(entry: dict[str, Any]) -> str:
    return entry["name"]


async def test_the_published_surface_matches_the_committed_snapshot() -> None:
    """Names, descriptions and input schemas, exactly as a client will receive them."""
    current = await _surface()
    serialised = json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    if os.environ.get("UPDATE_SNAPSHOTS"):  # pragma: no cover - maintenance path
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(serialised, encoding="utf-8")
        pytest.skip(f"snapshot rewritten: {SNAPSHOT}")

    assert SNAPSHOT.is_file(), (
        f"no snapshot at {SNAPSHOT}. Create it with "
        f"`UPDATE_SNAPSHOTS=1 pytest {Path(__file__).name}` and commit it."
    )
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == expected, (
        "the MCP surface changed. This is the contract every agent reads, so the "
        "diff deserves a look rather than a regenerate-and-commit. If the change "
        f"is intended: UPDATE_SNAPSHOTS=1 pytest {Path(__file__).name}"
    )


async def test_the_snapshot_is_not_quietly_empty() -> None:
    """A snapshot test that snapshots nothing passes forever. This is the guard on that."""
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert len(recorded["tools"]) == 8
    assert len(recorded["resources"]) == 1
    assert len(recorded["resource_templates"]) == 2
    for tool in recorded["tools"]:
        assert tool["description"], tool["name"]
        assert tool["input_schema"]["properties"], tool["name"]


async def test_every_declared_parameter_is_documented_in_the_description() -> None:
    """A parameter a model can pass but cannot read about is a trap.

    This project documents parameters in the tool description, under the
    `PARAMETERS` heading `test_server.py` requires — not in per-field
    descriptions — because a model reads the description as one block. So the
    check is that the two agree: every name the schema accepts appears in the
    prose that explains how to use it.
    """
    undocumented = []
    for tool in await build_server().list_tools():
        description = tool.description or ""
        parameters = description.partition("PARAMETERS")[2] or description
        undocumented.extend(
            f"{tool.name}.{name}"
            for name in tool.input_schema["properties"]
            if name not in parameters
        )
    assert not undocumented, (
        f"these parameters reach a model with no explanation: {undocumented}. "
        "Add them to the tool's PARAMETERS section."
    )


async def test_no_tool_requires_an_argument_a_model_cannot_guess() -> None:
    """Required parameters are the ones a model must invent on the first call.

    Keeping the list short and obvious — a bounding box, a column, a SQL
    statement — is what makes a tool usable without a round trip. A required
    parameter that is not in this set is worth arguing about.
    """
    expected_required = {
        "geoparquet_describe_source": set(),
        "geoparquet_preview_rows": set(),
        "geoparquet_filter_spatial": set(),
        "geoparquet_aggregate_attribute": {"group_by"},
        "geoparquet_summarize_h3": {"min_lon", "min_lat", "max_lon", "max_lat"},
        "geoparquet_find_nearest": {"lon", "lat"},
        "geoparquet_count_in_polygons": {"min_lon", "min_lat", "max_lon", "max_lat"},
        "geoparquet_run_sql": {"sql"},
    }
    actual = {
        tool.name: set(tool.input_schema.get("required", []))
        for tool in await build_server().list_tools()
    }
    assert actual == expected_required
