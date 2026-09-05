"""The demo command, checked the way the layering is checked.

`./scripts/demo.sh` is the first thing a reader runs and the only claim in the
README backed by pasted output. It broke once and nothing noticed: `cli.py`
was calling `tools.spatial.column_statistics`, a name the tool layer no longer
had, through a layer that requires a perimeter the CLI never installs. Both
failures are invisible to every other test in this suite, because nothing else
imports `cli` at all.

So the invariants are stated here rather than trusted:

  * every attribute the CLI reaches for on `engine` or `benchmark` exists;
  * the CLI does not go through `tools/`, because those handlers take their
    perimeter from an application that the CLI is not;
  * the demo passes an explicit scope to every operation it calls, which is
    the same discipline the application follows — resolve once, pass it down.

All three are hermetic: they read the CLI's syntax tree and never call it. The
end-to-end run is marked `network`, because the demo's fifth section measures
bytes over HTTP and there is nothing local to measure.

`serve` is checked differently, at the bottom of this file, because a syntax
tree cannot see what it got wrong. The command shipped calling `build_server()`
without installing a perimeter first: it started, announced its eight tools,
and failed every call that followed. Nothing it names is missing and nothing it
imports is forbidden, so every test above passes on it. Only running the
command and asking it a question finds that.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from geoparquet_mcp import cli
from geoparquet_mcp.engine import sources

CLI_PATH = Path(cli.__file__)
CLI_TREE = ast.parse(CLI_PATH.read_text(encoding="utf-8"), filename=str(CLI_PATH))


def _driven_modules() -> dict[str, object]:
    """The project modules the CLI imported, keyed by the name it calls them by.

    Derived from the CLI's own imports rather than listed here, so a new one
    is covered the day it is added instead of the day someone remembers to
    add it to this file.
    """
    driven: dict[str, object] = {}
    for node in ast.walk(CLI_TREE):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("geoparquet_mcp"):
            for alias in node.names:
                candidate = getattr(importlib.import_module(node.module), alias.name, None)
                if isinstance(candidate, ModuleType):
                    driven[alias.asname or alias.name] = candidate
    return driven


DRIVEN = _driven_modules()


def _function(name: str) -> ast.FunctionDef:
    for node in CLI_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from cli.py")


def _driven_calls(scope: ast.AST) -> list[tuple[str, ast.Call]]:
    """Calls written as `engine.thing(...)` or `benchmark.thing(...)`."""
    found = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id in DRIVEN:
            found.append((f"{target.id}.{node.func.attr}", node))
    return found


def test_every_name_the_cli_reaches_for_actually_exists() -> None:
    """The failure that broke the demo: a call to a function that had been renamed.

    Nothing else catches it, because no other test imports `cli`, and the
    traceback only appears once the command has already reached the network.
    """
    assert DRIVEN, "cli.py imports no geoparquet_mcp module; this test is checking nothing"
    missing = sorted(
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(CLI_TREE)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in DRIVEN
        and not hasattr(DRIVEN[node.value.id], node.attr)
    )
    assert not missing, (
        f"cli.py names things that do not exist: {', '.join(missing)}. "
        "The demo fails at runtime, and only after it has started reading the bucket."
    )


def test_the_cli_does_not_go_through_the_tool_layer() -> None:
    """The demo drives the engine, not the handlers.

    A tool handler takes its perimeter from `dependencies.current()`, which an
    application installs at startup. The CLI is not an application and installs
    nothing, so a handler called from here raises
    `DependenciesNotInstalledError` — correctly, and only at runtime.
    """
    imported = {
        node.module
        for node in ast.walk(CLI_TREE)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(CLI_TREE)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    offenders = sorted(name for name in imported if "geoparquet_mcp.tools" in name)
    assert not offenders, (
        f"cli.py imports the tool layer ({', '.join(offenders)}). Those handlers "
        "require a perimeter installed by an application; the CLI must call "
        "geoparquet_mcp.engine directly and pass a scope it resolved itself."
    )


def test_the_demo_passes_an_explicit_scope_to_every_operation() -> None:
    """Resolve once, pass it down — the same discipline `app.py` follows.

    Without it each call falls back to `default_scope()`, which re-resolves the
    release and hides the perimeter the demo is running under.
    """
    unscoped = [
        name
        for name, call in _driven_calls(_function("run_demo"))
        if not any(keyword.arg == "scope" for keyword in call.keywords)
    ]
    assert not unscoped, (
        f"these calls in run_demo() take no explicit scope: {', '.join(unscoped)}. "
        "The demo resolves one perimeter and hands it to every operation."
    )


def test_the_demo_still_reports_the_number_the_readme_quotes() -> None:
    """`pushdown_ratio` is the README's headline. The demo must still print it."""
    source = CLI_PATH.read_text(encoding="utf-8")
    for key in ("pushdown_ratio", "with_pushdown", "without_pushdown"):
        assert f'"{key}"' in source or f"'{key}'" in source, (
            f"run_demo() no longer reads {key!r} out of the pushdown report; "
            "the README quotes that section as pasted output."
        )


@pytest.mark.network
def test_the_demo_runs_end_to_end() -> None:
    """The whole command, against the remote dataset. What `./scripts/demo.sh` does."""
    assert cli.run_demo(as_json=True) == 0


# The two ways to reach the same stdio server: the console script Claude Desktop
# is configured with, and the CLI subcommand the project's own docs use. Both
# have to install the perimeter before serving, and only one of them used to.
STDIO_ENTRY_POINTS = {
    "geoparquet-mcp-server": ["-m", "geoparquet_mcp.server", "--transport", "stdio"],
    "geoparquet-mcp serve": ["-m", "geoparquet_mcp.cli", "serve", "--transport", "stdio"],
}

PROTOCOL_VERSION = "2025-06-18"


def _readline(stream: Any, timeout: float) -> str:
    """One line, or an assertion — never a test that hangs until CI gives up."""
    ready, _, _ = select.select([stream], [], [], timeout)
    assert ready, f"the server sent nothing within {timeout:.0f}s"
    return stream.readline()


def _ask_the_catalogue(command: list[str], timeout: float = 30.0) -> dict[str, Any]:
    """Start a stdio server, read `geoparquet://sources`, and return the reply.

    The catalogue is the cheapest question that still needs an installed
    perimeter: `catalog_document()` asks `dependencies.current()` for the scope
    and raises when nobody installed one. It touches no network, so this test
    is hermetic despite spawning a real server and speaking the real protocol.
    """
    process = subprocess.Popen(
        [sys.executable, *command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        # Pinned so startup resolves the release from the environment instead
        # of listing the bucket, which would make this test a network test.
        env={**os.environ, "GEOPARQUET_RELEASE": sources.OVERTURE_PINNED_RELEASE},
    )
    try:
        assert process.stdin is not None and process.stdout is not None

        def send(message: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test_cli", "version": "0"},
                },
            }
        )
        _readline(process.stdout, timeout)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "geoparquet://sources"},
            }
        )
        return json.loads(_readline(process.stdout, timeout))
    finally:
        # Closing stdin is how a stdio server is asked to stop; kill is the
        # fallback so a wedged server never outlives its test.
        process.stdin.close() if process.stdin else None
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a server that will not exit
            process.kill()


@pytest.mark.parametrize("entry_point", sorted(STDIO_ENTRY_POINTS))
def test_a_stdio_server_answers_once_it_is_launched(entry_point: str) -> None:
    """Launch it the way a client does, and ask it something.

    This is the test that was missing. `geoparquet-mcp serve` built the server
    and ran it without `dependencies.install(...)`, so every handler raised
    `DependenciesNotInstalledError` — a failure invisible to an AST walk,
    because the bug is a line that is not there.
    """
    reply = _ask_the_catalogue(STDIO_ENTRY_POINTS[entry_point])

    assert "error" not in reply, (
        f"`{entry_point}` served a catalogue it could not read: "
        f"{reply.get('error', {}).get('message')}. The entry point has to resolve the "
        "perimeter and install it before serving — see `server.main`."
    )
    document = json.loads(reply["result"]["contents"][0]["text"])
    assert document["release"] == sources.OVERTURE_PINNED_RELEASE
    assert set(document["sources"] and [entry["name"] for entry in document["sources"]]) == set(
        sources.SOURCES
    )
