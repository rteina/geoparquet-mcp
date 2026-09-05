# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv sync --extra dev                     # environment (Python >=3.12)

uv run pytest -m "not network" -q       # what CI runs: hermetic, ~2 s
uv run pytest -m network -q             # reads Overture's public bucket, ~2 min, breaks when a release expires
uv run pytest tests/test_engine_local.py::test_name -q   # one test
UPDATE_SNAPSHOTS=1 uv run pytest tests/test_protocol_surface.py   # rewrite the MCP surface snapshot

uv run ruff check .                     # lint (CI fails on this)
uv run ruff format --check .            # format check (CI fails on this too)

uv run geoparquet-mcp demo              # the end-to-end demo the README quotes
uv run geoparquet-mcp benchmark --only pushdown     # the 18.8× pushdown A/B
uv run geoparquet-mcp benchmark --only operations   # per-operation bytes/ms table
uv run geoparquet-mcp serve --transport stdio       # MCP server (delegates to server.main)
./scripts/make_fixtures.py --describe   # write the test corpus somewhere inspectable
```

`./scripts/demo.sh` and `./scripts/serve.sh` (`stdio` | `http` | `config`) are the zero-install paths
(both bootstrap `.venv/` with uv or `python3 -m venv` through `scripts/_common.sh`); prefer `uv run`
when the environment already exists. Both the demo and the `network` tests move real traffic
against `us-west-2` — don't run them in a loop.

Entry points: `geoparquet-mcp` (CLI), `geoparquet-mcp-server` (stdio MCP), `geoparquet-mcp-http` (FastAPI + mounted MCP).

## Architecture

The capability is a library; MCP and REST are two façades over it.

- `engine/` — the whole capability, with no protocol attached. Opens the DuckDB session, resolves
  dataset names to remote paths, builds the SQL, runs it, and reports bytes that crossed the network.
  It does not import `mcp`. `engine/__init__.py` is the public surface; everything else is internal.
  - `sources.py` — the registry and `DatasetScope`, the *only* thing that turns a dataset name into a
    path. No operation accepts a path. `resolve_release()` pins an Overture release and falls back to
    listing the bucket when the pin expires.
  - `session.py` — the process-wide DuckDB connection (`httpfs` + `spatial`, `h3` optional), plus the
    HTTP-log-based byte accounting that every result carries.
  - `operations.py` — the typed spatial operations. `query.py` — the read-only SQL escape hatch.
  - `errors.py` — the error vocabulary. Raw `duckdb.Error` never reaches a caller.
- `tools/`, `resources/` — MCP handlers. They validate nothing, compute nothing, hold no SQL.
- `server.py` — the only module allowed to import `mcp`. Wiring plus `build_server()`,
  `mcp_asgi_app()`, `mcp_lifespan()`.
- `app.py` — FastAPI app; REST routes call the same engine functions with the same arguments, and the
  MCP ASGI app is mounted inside it. Registered twice (a `Route` at `/mcp` *and* a `Mount` below it)
  because a bare mount would 307-redirect every `POST /mcp`. The MCP session manager's lifespan is
  chained into the app's by hand — a mounted ASGI app gets no startup event.
- `config.py` — `AppConfig.from_env()`, read once. Nothing downstream reads `os.environ`.
- `dependencies.py` — the adapter between protocol handlers and the engine. Imports neither `mcp` nor
  `fastapi`. The perimeter is resolved once at startup and `install()`ed into a plain module global
  (`_INSTALLED`); `using()` sets a ContextVar override for a narrower per-context scope. A ContextVar
  cannot hold the process default: a value set inside the lifespan task is invisible to request tasks.

Env vars: `GEOPARQUET_SOURCES` (narrow the perimeter), `GEOPARQUET_RELEASE`, `GEOPARQUET_ENABLE_MCP`,
`GEOPARQUET_MCP_PATH`, `GEOPARQUET_MCP_ALLOWED_HOSTS`.

## Invariants enforced by tests

Changes that violate these fail the suite; treat them as design constraints, not style preferences.

- **Only `server.py` imports `mcp`** (`test_layering.py`), and the engine must import in a process
  where `mcp` is unavailable.
- **Every tool handler is one `return` statement.** A second statement means logic drifted out of the
  engine into the protocol layer. Handlers must call `dependencies.engine_kwargs()` and must never
  name `DatasetScope` / `default_scope` / `restricted_to`.
- **No `duckdb` or `SELECT` anywhere under `tools/`** — including in docstrings and tool descriptions.
- **`tests/snapshots/mcp_surface.json`** pins every tool description and input schema. Any change to
  what a model sees must be an intentional snapshot rewrite.
- **`tests/test_cli.py`** walks `cli.py`'s AST: it may only name things the engine actually exports,
  may not reach through the tool layer, and the demo must pass an explicit scope to every call. It
  also launches both stdio entry points as subprocesses and reads a resource through each, because
  the perimeter-installing line an AST walk cannot see is the one that went missing once.
- **`tests/test_scope_isolation.py`** — 18 attempts to escape the perimeter (sibling datasets, parent
  escapes, prefix collisions, symlinks planted inside). `scope.assert_within()` is the one path that
  handles a path rather than a name.

## Testing conventions

`tests/corpus.py` generates a local GeoParquet corpus laid out exactly like Overture's — same nested
columns, same `bbox` struct, same directory shape — and `conftest.py` registers it in `sources.SOURCES`
so tests exercise the real resolution path (`AppConfig` → `dependencies.resolve` →
`DatasetScope.restricted_to` → operations) rather than hand-building a scope. The `local_session`
fixture deliberately loads `spatial` without `httpfs`, so a test that accidentally resolves a remote
path fails instead of quietly reading the bucket.

Mark anything that touches `us-west-2` with `@pytest.mark.network`. `asyncio_mode = "auto"` — async
tests need no decorator.

## Things worth knowing before changing the engine

- Spatial predicates are written as four independent comparisons on the `bbox` STRUCT members
  (`bbox.xmin <= … AND bbox.xmax >= …`), never as `ST_Intersects` on the geometry. Parquet keeps
  min/max statistics per column chunk, so a plain comparison prunes row groups from the footer; a
  spatial function is opaque and forces materialisation. Rewriting a filter to use geometry directly
  is correct and destroys the entire result. Arbitrary WKT is handled envelope-first, exact-second.
- The ~26.5 MB paid by the first query in a fresh process is Parquet footers, not data — it *is* the
  pruning mechanism. That's why the session is a process-wide singleton with its HTTP metadata cache on.
- `MAX_ROW_LIMIT = 1000` and the session's memory/thread/timeout ceilings are deliberate caps on what
  a careless query can cost.
- The SQL escape hatch is bounded by parsing, not by keyword blocklists: exactly one `SELECT`, every
  table reference resolving to a scope-registered view, and no table function at all (a table function
  is the only way to name a path in DuckDB SQL).

## Git conventions

`main` is the trunk: protected, always green, no direct pushes. Everything else is a short-lived
branch that merges back and is deleted.

### Branch names

```
<type>/<short-kebab-description>
<type>/<ticket>-<short-kebab-description>
```

Same `<type>` vocabulary as the commit types below. Lowercase ASCII, kebab-case, no underscores,
roughly 50 characters or less, no trailing `/` and no `..` (git refuses those refnames anyway). The
description names the outcome, not the file touched.

```
feat/point-in-polygon-tool
fix/uv-cache-key-race
refactor/engine-out-of-tool-layer
docs/readme-pushdown-measurement
ci/drop-node20-actions
chore/42-bump-duckdb
```

### Commit messages

Conventional Commits for the subject line, and the repository's existing prose discipline for
everything under it:

```
<type>(<scope>)!: <imperative subject, lowercase, no trailing period>

<body: why this change, what was actually broken, what a reader would otherwise
have to reconstruct from the diff. Wrapped at 80 columns.>

<footers, last: BREAKING CHANGE:, Refs:, Co-Authored-By:>
```

- **Types**: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, `revert`.
- **Scopes**, drawn from the layout: `engine`, `tools`, `resources`, `server`, `app`, `cli`, `config`,
  `deps`, `bench`, `tests`, `ci`, `docs`. Optional — omit it rather than invent one.
- **Breaking changes**: `!` before the colon *and* a `BREAKING CHANGE:` footer saying what a caller
  must now do differently. For this project that mostly means the MCP surface — a renamed tool, a
  changed input schema, a dropped parameter — since that is what a model is bound to.
- Subject ≤ 72 characters, imperative mood ("add", not "added"/"adds"). It completes the sentence
  "this commit will …".
- **The body is not optional for anything non-trivial.** The existing history is the reference: it
  states the failure and its cause, not a restatement of the diff. `fix(ci): pin setup-uv to v7`
  earns its body by explaining that `@v10` does not resolve because the action publishes floating
  major tags only up to v7.

How past commits map onto this:

```
feat(tools): expose the point-in-polygon join as an eighth tool
fix(ci): give each matrix leg its own uv cache key
refactor(engine): extract the spatial engine out of the MCP tool layer
docs: write the README around the measurement it can reproduce
```

A commit that rewrites `tests/snapshots/mcp_surface.json` is changing what every model sees; say so in
the body and say whether it was intended.
