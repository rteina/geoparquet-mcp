"""The architectural invariant, checked instead of asserted in a README.

The project's claim is that its capability is a library and MCP is one façade
over it. That claim is worth exactly as much as the import graph backing it,
and an import graph decays quietly: someone adds `from mcp.server import
Context` to a tool signature, everything still works, and the engine is no
longer usable without the protocol.

So the invariant is stated once, here, over the whole package: only
`server.py` may import `mcp`. The equivalent shell check is

    grep -rn "import mcp\\|from mcp" src/geoparquet_mcp/ \\
        | grep -v "^src/geoparquet_mcp/server.py:"
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "geoparquet_mcp"
# The one module allowed to know the protocol exists.
PROTOCOL_MODULE = PACKAGE_ROOT / "server.py"


def _python_files() -> list[Path]:
    return sorted(path for path in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_only_the_server_module_imports_mcp() -> None:
    offenders = [
        path.relative_to(PACKAGE_ROOT.parent)
        for path in _python_files()
        if path != PROTOCOL_MODULE and "mcp" in _imported_roots(path)
    ]
    assert not offenders, (
        "only server.py may import mcp; these modules break the engine/protocol "
        f"boundary: {', '.join(str(path) for path in offenders)}"
    )


def test_the_grep_that_documents_the_invariant_returns_nothing() -> None:
    """The literal shell check, so the README's command cannot drift from reality."""
    project_root = PACKAGE_ROOT.parent.parent
    found = subprocess.run(
        ["grep", "-rn", "import mcp\\|from mcp", "src/geoparquet_mcp/"],
        capture_output=True,
        text=True,
        cwd=project_root,
        check=False,
    ).stdout.splitlines()
    outside_server = [
        line for line in found if not line.startswith("src/geoparquet_mcp/server.py:")
    ]
    assert not outside_server, "\n".join(outside_server)


def test_the_engine_imports_with_mcp_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine must load in a process where `mcp` cannot be imported at all.

    A stronger check than the import graph: it proves the engine has no
    transitive dependency on the protocol either.
    """
    script = (
        "import sys\n"
        "class Blocker:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name == 'mcp' or name.startswith('mcp.'):\n"
        "            raise ImportError('mcp is blocked for this test')\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "from geoparquet_mcp import engine\n"
        "assert 'mcp' not in sys.modules\n"
        "print(len(engine.__all__))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert int(completed.stdout.strip()) > 0


def test_the_tool_layer_contains_no_sql() -> None:
    """Handlers delegate; the SQL lives in the engine.

    Checked by looking for SQL keywords in string literals, which is crude but
    catches the failure that actually happens: a "quick" query added to a
    handler because the engine did not quite have the shape someone wanted.
    """
    keywords = ("SELECT ", "FROM read_parquet", "GROUP BY", "parquet_metadata")
    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "tools").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for keyword in keywords:
            if keyword in text:
                offenders.append(f"{path.name}: {keyword!r}")
    assert not offenders, f"SQL found in the MCP tool layer: {'; '.join(offenders)}"


def test_the_dependency_arrow_points_one_way() -> None:
    """`tools` and `resources` may import `engine`. Never the reverse.

    The boundary is only worth something if it has a direction: an engine that
    reaches back into the protocol layer is the same coupling wearing a
    different import.
    """
    forbidden = ("geoparquet_mcp.tools", "geoparquet_mcp.resources", "geoparquet_mcp.server")
    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "engine").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.name} -> {module}" for module in forbidden if module in text)
    assert not offenders, f"the engine imports its own callers: {'; '.join(offenders)}"


def test_the_engine_surface_is_importable_on_its_own() -> None:
    module = importlib.import_module("geoparquet_mcp.engine")
    for name in ("bbox_query", "h3_aggregate", "point_in_polygon", "DatasetScope"):
        assert hasattr(module, name), f"{name} missing from the engine's public surface"
