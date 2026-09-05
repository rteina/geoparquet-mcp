# geoparquet-mcp

An MCP server that answers spatial questions about multi-gigabyte GeoParquet files sitting on public
object storage, without downloading or importing them first.

[![CI](https://github.com/rteina/geoparquet-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/rteina/geoparquet-mcp/actions/workflows/ci.yml)

## The problem

Cloud-native geospatial data has largely settled on Parquet on object storage. The tools that query
it have not: most of them still want a database. So the path from "there is a 10 GB `places` file in
a bucket" to "how many restaurants are in this arrondissement" runs through an import — provision
PostGIS, load 73 million rows, index them, keep the copy in step with the next monthly release.

For a person that import is a chore. For an agent it is a wall: it cannot provision a database
mid-conversation, so it falls back on downloading the file and filtering it locally — moving ten
gigabytes to answer a question whose answer is four kilobytes.

DuckDB already reads remote Parquet over HTTP range requests and pushes filters down into the file.
This project is that capability wrapped in the protocol an agent already speaks, with the perimeter
of what it may read resolved once, at startup, by the application rather than by the tool.

## Demo

```sh
./scripts/demo.sh
```

One command from a fresh clone. The script prepares its own environment into `.venv/` — [uv](https://docs.astral.sh/uv/)
when it is on `PATH`, otherwise `python3 -m venv` plus pip on a local Python 3.12+ — so there is
nothing to install first. Expect about a minute and ~200 MB of traffic, most of it the deliberately
unoptimised comparison in section 5.

Real output, against Overture Maps release `2026-08-19.0`:

```
geoparquet-mcp demo — spatial analysis on a remote file, no import step

1. The dataset, described from its footers
────────────────────────────────────────────────────────────────────────
source        Overture Maps — places
licence       CDLA-Permissive-2.0 (data); ODbL applies to OpenStreetMap-derived records
release       2026-08-19.0
location      https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/release/2026-08-19.0/theme=places/type=place/
size          73,631,092 rows, 16 files, 10.48 GB, 4,096 row groups
read to learn all of that: 26.5 MB

2. What is in Paris, France?
────────────────────────────────────────────────────────────────────────
     5,045  french_restaurant
     5,019  (uncategorised)
     4,869  professional_services
     4,216  community_services_non_profits
     3,336  restaurant
     3,306  parking
     3,192  hotel
     2,700  grocery_store
166,966 features, 1,022 distinct categories.primary values
read: 4.0 MB in 6,674 ms

3. Bakeries within 400 m of Notre-Dame
────────────────────────────────────────────────────────────────────────
     152 m  A. Lacroix Pâtissier
     197 m  Aux Fontaines de Chocolat
     205 m  Hure, Createur de Plaisir
     232 m  Boulangerie Pâtisserie Uré Île De La Cité
     254 m  Cookies By Moon's
read: 1.1 MB in 2,874 ms

4. Where is it densest? H3 cells, computed remotely
────────────────────────────────────────────────────────────────────────
     3,906  881fb475b5fffff  (48.8710, 2.3024)
     3,518  881fb4662dfffff  (48.8622, 2.3477)
     3,207  881fb46667fffff  (48.8701, 2.3440)
     3,050  881fb46665fffff  (48.8723, 2.3327)
     2,660  881fb46629fffff  (48.8679, 2.3553)
5 cells at resolution 8
read: nothing — already in the session cache (594 ms)

5. The point of all this: bytes moved
────────────────────────────────────────────────────────────────────────
matching features            166,966
whole dataset, if downloaded 10.48 GB (16 files)
one part file, if downloaded 667.9 MB
same query, pushdown OFF     128.8 MB in 25,033 ms
same query, pushdown ON      6.9 MB in 4,626 ms

  18.8× fewer bytes than the same query without pushdown
  97.5× fewer bytes than downloading that one file
  1,530× fewer bytes than downloading the dataset

attribution: © Overture Maps Foundation — CDLA-Permissive-2.0 (data); ODbL applies to OpenStreetMap-derived records
```

The only thing that landed on disk is the project's own `.venv/`. There is no database, no import
step and no copy of the data: the 10.48 GB stayed in `us-west-2`, and the five sections above moved
about 200 MB of it, nearly all in the last one.

## Running the server

The demo is a command, not a server: it answers five questions and exits. Connecting an agent to the
same engine is a different script, with the same bootstrap — nothing to install first.

```sh
./scripts/serve.sh          # MCP over stdio, the transport a desktop client launches
./scripts/serve.sh http     # MCP at POST /mcp plus the REST routes, on :8000
./scripts/serve.sh config   # the claude_desktop_config.json block for this clone
./scripts/serve.sh help
```

`stdio` sits silent, waiting for JSON-RPC on stdin; that is what a working server looks like, and
Ctrl+C ends it. Bootstrap messages go to stderr precisely so stdout stays a clean protocol channel.

For Claude Desktop you never run it yourself — the app launches the server as a subprocess. The
awkward part is that the configuration has to name the executable by absolute path, because the app
does not read your shell profile, so `./scripts/serve.sh config` prints the block with that path
already filled in:

```json
{
  "mcpServers": {
    "geoparquet": {
      "command": "/absolute/path/to/geoparquet-mcp/.venv/bin/geoparquet-mcp-server",
      "args": ["--transport", "stdio"]
    }
  }
}
```

Paste it into `claude_desktop_config.json`, restart, and the eight tools appear under the connector.
[`docs/claude-desktop.md`](docs/claude-desktop.md) has the rest: the environment variables that narrow
what the process may read, what the first query costs, and what to check when the connector does not
appear.

The script is a wrapper over the entry points, which are what you would run in a deployment:

```sh
uv run geoparquet-mcp-server --transport stdio                    # or: geoparquet-mcp serve
uv run uvicorn geoparquet_mcp.app:create_app --factory --port 8000
```

## The measurement

Section 5 above is the whole argument, and it has its own command:

```sh
uv run geoparquet-mcp benchmark --only pushdown
```

It runs the same aggregate twice against the same remote Parquet part — once normally, once with
DuckDB's `filter_pushdown` optimiser disabled — each on its own cold DuckDB session so neither warms
the other:

```
scan target                  s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/part-00007-…-c000.zstd.parquet
matching features            166,966
whole dataset, if downloaded 10,480,684,059 bytes (16 files)
one part file, if downloaded    667,940,013 bytes
that part's Parquet footer        1,644,542 bytes (read by both runs)
same query, pushdown OFF        128,806,432 bytes in 24,989 ms
same query, pushdown ON           6,850,570 bytes in 4,349 ms

  18.8× fewer bytes than the same query without pushdown
```

**6,850,570 bytes against 128,806,432 — 18.8×.** The filter went down into the remote file. The
dataset was not fetched and then filtered; the row groups that could not match were never requested,
because their footer statistics ruled them out before a single data page was asked for.

Two things about that number are worth stating before a careful reader finds them.

**The ~26 MB floor is footers, not data.** Every first query in a fresh process pays about 26.5 MB
before it reads anything useful: the Parquet footers of all 16 parts, carrying 4,096 row groups of
statistics. That looks like a terrible fixed cost until you see what it buys — it *is* the pruning
mechanism. DuckDB reads those statistics to decide which row groups to skip, and then fetches only
the surviving column chunks. Per-operation, measured with:

```sh
uv run geoparquet-mcp benchmark --only operations
```

that command prints a table with the two costs in separate columns. The `+footer MB` column is
26.49 MB on every row that reads `places` — the same 26.5 MB the demo reports in section 1 above. The
`query MB` column is what the operation itself costs once those footers are cached: 0.80 MB to 5.62 MB
across the ten operations. The `bbox_query (GeoJSON)` row — 50 features returned with true geometry —
is **2,972 ms** and 4.33 MB, as a median over five runs, each starting from a fresh DuckDB session.

Because the session is a process-wide singleton with its HTTP metadata cache on, that toll is paid
once and every later query is the small number. Run the command yourself and the byte columns should
match; the millisecond columns will not, because they are mostly your link to `us-west-2`.

**The predicates are on the four `bbox` struct members, not on the geometry.** Overture stores a
`STRUCT(xmin, xmax, ymin, ymax)` alongside each feature, and the filter is written as four
independent comparisons on those four columns:

```sql
bbox.xmin <= 2.47 AND bbox.xmax >= 2.20 AND bbox.ymin <= 48.91 AND bbox.ymax >= 48.80
```

An `ST_Intersects` over the geometry column would be equally correct and would destroy the result.
Parquet keeps min/max statistics per column chunk, so a plain comparison on `bbox.xmin` is something
the reader can evaluate against a footer; a spatial function is an opaque call it must materialise
rows to run. No pruning, no argument. Where a caller passes an arbitrary WKT geometry, the envelope
prunes the read and the exact shape then filters the survivors — so the answer stays exact and the
read stays cheap.

## Architecture decision: MCP as an ASGI sub-application

**Context.** The same operations need two audiences: agents over MCP, and ordinary HTTP clients over
REST. One implementation, two protocols. The question is how the MCP endpoint gets into the process.

**Options.**

*(a) MCP mounted as an ASGI sub-application inside the FastAPI app.* One process, one DuckDB session
and its warm footer cache, one lifespan. The cost is real: the two protocols cannot be scaled or
restarted independently, and a mounted ASGI app gets no startup event from the parent router — its
session manager has to be chained into the host's lifespan by hand, and forgetting to is a failure
that shows up only when the first MCP request arrives.

*(b) A separate MCP service proxying the REST API.* The clean separation: each protocol scales on its
own, and a crash in one does not take the other with it. It buys that with a second deployable to run
and version, a network hop on every tool call, and two caches instead of one — the MCP process holds
no DuckDB session, so the footer warmth that makes the second query fast lives entirely in the API
tier and the proxy pays for its own serialisation on both legs. It is the right answer for a service
with independent traffic profiles for the two protocols.

*(c) A sidecar process.* Keeps the two protocols isolated without a network round-trip to a
separately deployed service, and lets the MCP side crash and restart alone. It needs an IPC channel,
a serialisation format across it, and a supervisor that starts both and knows what to do when one
dies. That is a small distributed system, and it inherits the failure modes of one.

**Decision: (a).** The constraint that settled it is deployment. This server has to run as a single
command on someone's laptop — Claude Desktop launches it as a subprocess over stdio, with no
orchestrator, no supervisor and no service mesh. A proxy means a second process the user has to start
and keep in step. A sidecar means IPC and a supervisor for a workload that fits in one process. A
sub-application means one process, and the wiring is one file: [`src/geoparquet_mcp/app.py`](src/geoparquet_mcp/app.py).
[`docs/architecture-c4.md`](docs/architecture-c4.md) draws that decision, and the rest of the
structure, as a C4 model — context, containers, components, code.

That same file also has to attach the ASGI app twice — a `Route` at `/mcp` and a `Mount` for
anything below it — because Starlette compiles a mount to a pattern requiring a segment after the
prefix, so a bare `POST /mcp` would only ever be answered by a 307 to `/mcp/`. Streamable HTTP is a
single endpoint, not a tree; that redirect would be friction on every request a client makes.

**Consequences, including the bad ones.**

- The two protocols share a lifecycle. Restarting to pick up a REST change restarts every MCP
  session with it. They cannot be scaled apart: if MCP traffic grows and REST does not, the only
  lever is more copies of both.
- They share a DuckDB session, which is the point — the warm footer cache is what makes the second
  query fast — and also a shared failure domain. A query that exhausts memory takes down both façades.
- MCP is switched off with `GEOPARQUET_ENABLE_MCP=0`, and then the sub-application is never built and
  the route is never registered. `/mcp` returns FastAPI's own 404 because nothing is there, not
  because a handler decided to refuse. That is the cheap version of independent deployment: a
  REST-only process is one environment variable away. The reverse — MCP without REST — is the stdio
  entry point, which is a different process shape entirely.
- The chained lifespan is load-bearing and easy to break. It is covered by a test rather than a
  comment.

## The tools implement nothing

The engine — `src/geoparquet_mcp/engine/` — is the entire capability. It opens the DuckDB session,
resolves dataset names to paths, builds the SQL, runs it, and reports the bytes that crossed the
network. It does not import `mcp`. It can be used from a notebook and tested without a protocol, and
most of the test suite does exactly that.

The tool layer is handlers. Here is one, complete:

```python
def describe_source(source: str = engine.DEFAULT_SOURCE) -> dict[str, Any]:
    """Return one dataset's schema, CRS, extent and physical footprint."""
    return engine.dataset_schema(source=source, **dependencies.engine_kwargs())
```

That is the whole function. All eight are this shape, and the shape is enforced: a test walks the AST
of every registered handler and fails if the body is anything other than a single `return`. Not as a
style rule — the day a handler grows a second statement, the reason is always that logic has drifted
out of the engine and into the protocol layer, where it can no longer be used or tested without MCP.
Sibling tests assert that no file under `tools/` contains SQL or a DuckDB reference, and that only
`server.py` imports `mcp`.

Why it matters: the REST routes in `app.py` call the same engine functions with the same arguments.
Adding a third protocol would duplicate no logic — it would be another file of handlers next to the
two that exist. The 400-word tool descriptions that teach a model when *not* to call a tool live in
the handler modules, because they are protocol surface; the behaviour they describe does not.

## Isolation

A `DatasetScope` is the only thing in the engine that turns a dataset *name* into a readable *path*,
and no operation accepts a path. It is resolved once, when the application starts, and injected into
handlers through `dependencies.py`. A handler has no argument through which a different scope could
arrive and no constructor to call, so the strongest thing it can do is narrow the perimeter for
itself. Widening is not refused at runtime — it is unreachable. Set `GEOPARQUET_SOURCES=overture_places`
and the other two datasets do not exist as far as any tool is concerned.

The general point, and the reason this is in the README rather than in a docstring: when an agent
calls a tool, the authorisation decision has to come down the same path it does for any other
request. A tool layer that resolves its own perimeter is a second authorisation implementation, and
a second one is one that will drift from the first. `dependencies.py` imports neither `mcp` nor
`fastapi`; it is the adapter between the two, and both protocols end up holding the identical object.

The one code path that handles a path rather than a name — the benchmark, which pins a single Parquet
part to keep its unpushed comparison affordable — goes through `scope.assert_within()`, which checks
it against prefixes the scope built itself. Eighteen tests in `tests/test_scope_isolation.py` try to
get past it: a sibling dataset under the same root, a parent-directory escape, an escape dressed up
as a deeper path, a prefix that merely starts the same, a non-Parquet object inside the perimeter, a
symlink planted inside it pointing out.

## Tests

```sh
uv run pytest -m "not network"   # 229 tests, 3 s — what CI runs
uv run pytest -m network         #  19 tests against the live Overture dataset
```

248 tests, split by a `network` marker, because the two halves fail for different reasons.

The default half is hermetic. It generates a small GeoParquet corpus on disk laid out exactly like
Overture's — same nested column names, same `bbox` struct, same directory shape — and runs the real
engine against it, asserting exact results. It needs no network, finishes in three seconds, and is
what runs on every push, on Python 3.12 and 3.13. `./scripts/make_fixtures.py` writes the corpus out if
you want to look at it.

The `network` half reads Overture's public bucket. It is the only place the byte-level pushdown claim
can be measured, and it breaks whenever a release expires — a failure that says nothing about the
change under review. It runs on a weekly cron and on manual dispatch, never on a pull request.

The demo above is held in place by the same kind of test. It broke once — `cli.py` went on calling a
tool-layer function that had been renamed, through a layer that needs a perimeter the CLI never
installs — and nothing caught it, because nothing imported `cli` at all. `tests/test_cli.py` now
reads the CLI's syntax tree and fails if it names something the engine does not have, if it reaches
through the tool layer, or if the demo stops passing an explicit scope. It runs in 0.24 seconds and
would have caught both halves of that failure before the command ever touched the network.

A syntax tree has a blind spot, though, and `geoparquet-mcp serve` sat in it. The subcommand built
the server and ran it without installing a perimeter first, so it started, announced its eight tools,
and failed every call that followed. It named nothing that did not exist and imported nothing it
should not have: the bug was a line that was not there. So the same file now launches both stdio
entry points as subprocesses and reads a resource through each, over real pipes — the only way to
find out that a server serves. That costs about a second, and it is the second the rest of the suite
was missing.

Among the hermetic tests is a snapshot of the complete JSON of the MCP surface — all eight tools, one
resource and two resource templates, with every description and input schema. It fails on any
unintended change to what a model sees, which is the part of this project a refactor is most likely
to alter silently. Alongside it: a test that every declared parameter is documented in its tool's
description, and one that no tool requires an argument a model cannot guess.

## What this is not

- **Not multi-tenant.** One perimeter per process, resolved at startup. The machinery for a
  per-request scope exists (`dependencies.using()`) and is used by tests, but nothing calls it in
  production and no request carries a tenant.
- **Not writable.** Every operation is a read. The SQL escape hatch refuses anything that is not
  exactly one `SELECT`, then walks the parsed tree and refuses any table reference that is not a
  dataset in scope — so it is bounded by the same perimeter as every other tool, not by a keyword
  blocklist.
- **Not authenticated.** There is no auth layer at all. Do not put this on a public interface.
- **Not for sensitive data.** It reads anonymous public buckets. There is no credential handling, no
  encryption story, and no audit log.
- **Tested against one dataset family.** Overture Maps `places`, `divisions` and `buildings`. The
  engine assumes a GeoParquet-conventional `bbox` struct with row-group statistics; a file without
  one will be read correctly and pruned not at all, which turns the central claim off without
  announcing it.
- **A demonstration of architecture, not a product.** Version 0.1.0, one author, no users. It exists
  to be read and to have its numbers re-run, not to be deployed.

## Keeping the demo alive

Overture publishes a release roughly monthly and keeps only about two on the public bucket; objects
carry a 60-day retention rule. A hard-coded release path in a README is therefore a demo with a
two-month shelf life, and a `404` from a third-party bucket is a bad first impression to hand someone
who cloned your repository in good faith.

`resolve_release()` pins a release that was verified working (`2026-08-19.0`, on 2026-09-05 with
DuckDB 1.5.5) and falls back to listing the bucket and taking the newest when the pin is gone.
Discovery failures fall back to the pin rather than raising, because a stale pin produces a clearer
error later than a network error at startup. The weekly `network` CI job exists to notice the
rotation before a reader does.

## What would come next

**Iceberg.** The obvious one, and the one that would change the shape of the argument rather than
extend it. Today pruning is a property of how Overture happened to lay its files out: 16 parts,
4,096 row groups, and a bbox column whose statistics happen to be well-clustered geographically. An
Iceberg table moves that decision into the table format — snapshots and hidden partitioning mean the
planner prunes from the manifest before touching a data file at all, and the ~26 MB footer toll
becomes a manifest read instead. What I have read but not built is how a spatial predicate pushes
through the manifest: partition transforms are defined over scalar columns, and a bounding box is
four of them plus a claim about their relationship. Whether that is expressible as partition
statistics, or needs the geometry-type support that is still landing in the spec, is the part I would
have to find out by doing it.

**Persisting the footer statistics.** 26.5 MB per cold process is fine for a long-lived server and
poor for a desktop client that starts, answers three questions and exits. The same toll is why
`overture_buildings` — 513 parts, ~277 GB — is a minute-scale query rather than an interactive one,
even though pushdown cuts a city query on it to well under a gigabyte. Caching the row-group
statistics to disk would make a cold start nearly free. The open question is invalidation: the cache
is keyed on a release path that expires, so the cache and `resolve_release()` have to agree about
what "current" means, and getting that wrong means silently querying statistics for files that are no
longer there.

**Measuring how much of the win is Overture's file layout.** The 18.8× is real and reproducible, but
it is a measurement of this engine *against this dataset*. A Parquet file whose rows are in ingestion
order rather than spatially clustered has row-group bboxes that all span the planet, and prunes
nothing. Quantifying that — the same query against the same data written with and without a Hilbert
sort — would turn a number that is currently a demonstration into one that predicts something.

## Licence

The project is MIT — see [LICENSE](LICENSE).

The data is not mine and is not covered by that. Overture Maps `places` is
**CDLA-Permissive-2.0**; `divisions` and `buildings` are ODbL or CDLA-Permissive-2.0 depending on the
contributing source. Attribution: **© Overture Maps Foundation**. Records derived from OpenStreetMap
carry ODbL obligations of their own. Every catalogue entry and every tool response reports the licence
of the data it read.
