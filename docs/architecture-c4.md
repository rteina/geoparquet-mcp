# Architecture, as a C4 model

Four diagrams at four zoom levels, following the [C4 model](https://c4model.com/): context, then
containers, then components, then code. Each level is the previous one with one box opened, so a
reader can stop at the depth their question needs.

The README already argues *why* this is built the way it is — MCP as an ASGI sub-application, tools
that implement nothing, a perimeter resolved once at startup. This document is the map that goes with
that argument: what the boxes are, which of them may talk to which, and which test fails when an
arrow appears that should not exist.

## How to read the diagrams

C4's own notation is a rectangle with a name, a technology, and a sentence of purpose, plus labelled
arrows that say what crosses the boundary. These use Mermaid flowcharts rather than Mermaid's
experimental `C4Context` syntax, so they render on GitHub as-is; the colours carry the C4 meaning:

| Colour | Element |
| --- | --- |
| dark blue | person — a human user |
| medium blue | software system in scope |
| grey | external system — someone else's, and not ours to change |
| lighter blue | container — a separately runnable process |
| pale blue | component — a grouping of code inside one container |

One caveat worth stating up front, because it makes level 2 look thin: this project is a single
process by design. There is no service mesh to draw, and the interesting structure lives at level 3.

---

## Level 1 — System context

Who uses this, and what it depends on.

```mermaid
flowchart TB
    user["<b>Analyst or developer</b><br/><i>[Person]</i><br/>Asks spatial questions about<br/>data they have not downloaded"]
    agent["<b>MCP client</b><br/><i>[Software system]</i><br/>Claude Desktop, MCP Inspector,<br/>or any agent runtime"]
    rest["<b>HTTP client</b><br/><i>[Software system]</i><br/>curl, a notebook,<br/>another service"]

    sys["<b>geoparquet-mcp</b><br/><i>[Software system]</i><br/>Answers spatial questions about multi-gigabyte<br/>GeoParquet on object storage by reading it in place.<br/>No import, no database, no copy."]

    overture["<b>Overture Maps public bucket</b><br/><i>[External system]</i><br/>s3://overturemaps-us-west-2<br/>~292 GB of GeoParquet, anonymous reads,<br/>a new release monthly and only two retained"]
    extrepo["<b>DuckDB extension repositories</b><br/><i>[External system]</i><br/>httpfs and spatial from the core repository,<br/>h3 from the community one"]

    user -->|"asks a question in<br/>natural language"| agent
    user -->|"calls an endpoint<br/>directly"| rest
    agent -->|"tool calls and resource reads<br/>[MCP over stdio or streamable HTTP]"| sys
    rest -->|"GET /sources, /query/*<br/>[JSON over HTTP]"| sys
    sys -->|"reads Parquet footers, then only the<br/>surviving column chunks<br/>[HTTP range requests]"| overture
    sys -->|"INSTALL and LOAD, once per session<br/>[HTTPS]"| extrepo

    classDef person fill:#08427b,stroke:#052e56,color:#ffffff
    classDef system fill:#1168bd,stroke:#0b4884,color:#ffffff
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    class user person
    class sys system
    class agent,rest,overture,extrepo external
```

Three things this level is meant to settle:

- **The data never enters the system.** The only arrow that carries bulk bytes points *at* the bucket,
  and what comes back is footers and the column chunks a filter could not rule out. There is no
  ingestion path, no scheduled sync, and no local copy to invalidate.
- **The two client kinds are peers.** An agent over MCP and a script over REST reach the same
  operations with the same arguments; neither is a wrapper around the other.
- **There is no authentication anywhere in the picture.** No identity provider, no credential store,
  no per-user perimeter. The bucket is public and the server is not. That is a stated limitation,
  not an omission from the diagram — see *What this is not* in the README.

---

## Level 2 — Containers

Zooming into `geoparquet-mcp`. A C4 container is something separately runnable; here, three
executables that are three shapes of the same code.

```mermaid
flowchart TB
    agent["<b>MCP client</b><br/><i>[Software system]</i>"]
    rest["<b>HTTP client</b><br/><i>[Software system]</i>"]

    subgraph sys["geoparquet-mcp"]
        direction TB
        stdio["<b>geoparquet-mcp-server</b><br/><i>[Container: Python process]</i><br/>MCP over stdio, launched as a subprocess<br/>by a desktop client. Resolves and installs<br/>the perimeter itself — no lifespan to do it."]
        http["<b>geoparquet-mcp-http</b><br/><i>[Container: Python process, uvicorn + FastAPI]</i><br/>REST routes, plus the MCP streamable-HTTP<br/>endpoint mounted inside the same app at /mcp"]
        cli["<b>geoparquet-mcp</b><br/><i>[Container: CLI process]</i><br/>demo, benchmark, sources — the commands that<br/>produce the numbers in the README. Its serve<br/>subcommand delegates to the stdio entry point<br/>above rather than starting a server of its own."]
        engine["<b>engine + DuckDB</b><br/><i>[Library, in-process — not a deployable]</i><br/>The whole capability. Opens the DuckDB session,<br/>resolves names to paths, builds and runs the SQL,<br/>and reports the bytes that crossed the network."]
    end

    overture["<b>Overture Maps public bucket</b><br/><i>[External system]</i>"]

    agent -->|"JSON-RPC over stdin/stdout<br/>[MCP stdio]"| stdio
    agent -->|"POST /mcp<br/>[MCP streamable HTTP]"| http
    rest -->|"[JSON over HTTP]"| http
    stdio -->|"in-process calls"| engine
    http -->|"in-process calls"| engine
    cli -->|"in-process calls"| engine
    engine -->|"[HTTP range requests]"| overture

    classDef system fill:#1168bd,stroke:#0b4884,color:#ffffff
    classDef container fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef library fill:#438dd5,stroke:#2e6295,color:#ffffff,stroke-dasharray: 5 3
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    class stdio,http,cli container
    class engine library
    class agent,rest,overture external
```

The dashed box is the honest part. `engine/` is not a container — it is never deployed on its own and
has no address — but drawing it at this level is the only way to show that the three containers are
alternatives rather than collaborators. **No arrow runs between the three processes.** You start one
of them; whichever you start links the same engine into its own address space, opens its own DuckDB
session, and reads the same bucket.

That is also where the architecture decision recorded in the README lands on the diagram. Option (b),
a separate MCP service proxying REST, would have made `stdio` and `http` two boxes with a network
arrow between them and two DuckDB sessions behind it. Option (a) keeps one box, one session, and one
warm footer cache — at the cost of one lifecycle for both protocols.

---

## Level 3 — Components

### 3a. Inside `geoparquet-mcp-http`

The container with the most structure: both façades, the adapter between them, and the engine as a
single box that the next diagram opens.

```mermaid
flowchart TB
    agent["<b>MCP client</b><br/><i>[Software system]</i>"]
    rest["<b>HTTP client</b><br/><i>[Software system]</i>"]

    subgraph proc["Container: geoparquet-mcp-http"]
        direction TB

        app["<b>app.py — FastAPI application</b><br/><i>[Component: FastAPI]</i><br/>REST routes /health, /sources, /query/*.<br/>Attaches the MCP ASGI app twice — a Route at /mcp<br/>and a Mount below it — and chains the MCP session<br/>manager's lifespan into its own."]
        srv["<b>server.py — MCP wiring</b><br/><i>[Component: MCP SDK]</i><br/>The only module allowed to import mcp.<br/>build_server, mcp_asgi_app, mcp_lifespan, and the<br/>proxy that turns an EngineError into a ToolError<br/>so its text reaches the model."]
        tools["<b>tools/</b><br/><i>[Component: handlers]</i><br/>discovery, spatial, sql — eight tools, each one<br/>a single return statement. No SQL, no duckdb,<br/>not even in the prose."]
        res["<b>resources/catalog.py</b><br/><i>[Component: handlers]</i><br/>geoparquet://sources and two URI templates.<br/>The catalogue is a document, so it is a resource<br/>rather than a tool."]
        conf["<b>config.py — AppConfig</b><br/><i>[Component]</i><br/>from_env, read once. Whether MCP is mounted,<br/>which datasets are in scope, which release is pinned.<br/>Nothing downstream reads os.environ."]
        deps["<b>dependencies.py</b><br/><i>[Component: adapter]</i><br/>Resolves the perimeter once at startup and installs it.<br/>Imports neither mcp nor fastapi. A process default in a<br/>plain global, a per-context override in a ContextVar."]
        engine["<b>engine/</b><br/><i>[Component: the capability]</i><br/>Spatial operations over remote GeoParquet.<br/>Opened at level 3b."]
    end

    overture["<b>Overture Maps public bucket</b><br/><i>[External system]</i>"]

    agent -->|"POST /mcp"| app
    rest -->|"GET /sources, /query/*"| app
    app -->|"delegates the /mcp route<br/>to the mounted ASGI app"| srv
    app -->|"reads at startup"| conf
    app -->|"resolve + install in the lifespan;<br/>Depends(current) per request"| deps
    srv -->|"registers at build time"| tools
    srv -->|"registers at build time"| res
    srv -->|"resolve + install<br/>(stdio entry point only)"| deps
    app -->|"calls operations directly,<br/>same functions, same arguments"| engine
    tools -->|"one call per handler"| engine
    res -->|"catalogue and schema"| engine
    tools -->|"engine_kwargs"| deps
    res -->|"current().scope"| deps
    deps -->|"builds the DatasetScope<br/>and the shared Session"| engine
    engine -->|"[HTTP range requests]"| overture

    classDef component fill:#85bbf0,stroke:#5d82a8,color:#000000
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    class app,srv,tools,res,conf,deps,engine component
    class agent,rest,overture external
```

Read the arrows into `engine` and the shape of the claim falls out: `app.py` and `tools/` both point
at it, and neither points at the other. The REST routes are not calling the MCP tools, and the tools
are not calling the routes. Adding a third protocol adds a third arrow into the same box.

The `stdio` container is this diagram minus `app.py` and `conf` as a FastAPI concern: `server.main()`
resolves the configuration, installs the same dependencies, and runs the same registered handlers
over stdin/stdout. Two lines of deliberate duplication, chosen over a handler that silently falls
back to the default perimeter when nobody installed one.

### 3b. Inside `engine/`

The capability, with no protocol attached.

```mermaid
flowchart TB
    callers["<b>tools/, resources/, app.py, cli.py</b><br/><i>[Components: callers]</i>"]

    subgraph eng["Component: engine/"]
        direction TB
        ops["<b>operations.py</b><br/><i>[Sub-component]</i><br/>The typed operations: schema, extent, preview,<br/>spatial_filter, nearest, attribute_aggregate,<br/>h3_aggregate, point_in_polygon, column_statistics.<br/>Pydantic models validate the input; the SQL is built here."]
        query["<b>query.py</b><br/><i>[Sub-component]</i><br/>The read-only SQL escape hatch. Bounded by parsing:<br/>exactly one SELECT, every table reference a scope-registered<br/>view, and no table function at all."]
        src["<b>sources.py</b><br/><i>[Sub-component]</i><br/>The registry and DatasetScope: the only thing that turns<br/>a dataset name into a path. resolve_release pins a release<br/>and falls back to listing the bucket when the pin expires."]
        sess["<b>session.py</b><br/><i>[Sub-component]</i><br/>The process-wide DuckDB connection, its extensions and its<br/>ceilings, plus the byte accounting every result carries.<br/>measure attributes HTTP GETs by connection id and query watermark."]
        err["<b>errors.py</b><br/><i>[Sub-component]</i><br/>The error vocabulary. A raw duckdb.Error never<br/>reaches a caller."]
    end

    duckdb["<b>DuckDB</b><br/><i>[External: embedded engine]</i><br/>httpfs + spatial, h3 optional.<br/>Reads remote Parquet over range requests<br/>and pushes filters into the footer statistics."]
    overture["<b>Overture Maps public bucket</b><br/><i>[External system]</i>"]

    callers -->|"operation(source=..., scope=..., session=...)"| ops
    callers -->|"run_sql(sql=..., scope=..., session=...)"| query
    ops -->|"scope.get / scope.target:<br/>name to read_parquet target"| src
    query -->|"view_names, then one view<br/>per dataset in scope"| src
    ops -->|"session.measure"| sess
    query -->|"session.measure"| sess
    ops -->|"raises"| err
    query -->|"raises"| err
    sess -->|"raises, after stripping<br/>the SQL echo"| err
    sess -->|"SQL on a private cursor"| duckdb
    duckdb -->|"footers first, then only the<br/>surviving column chunks"| overture

    classDef component fill:#85bbf0,stroke:#5d82a8,color:#000000
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    class ops,query,src,sess,err component
    class callers,duckdb,overture external
```

Two arrows carry the design.

**`operations.py` → `sources.py`** is the isolation boundary. No operation takes a path; it takes a
name and asks the scope. A scope can be narrowed and never widened, so a caller holding a restricted
one has no argument through which a wider perimeter could arrive.

**`operations.py` → `session.py`** is why the numbers exist. Every operation runs inside
`session.measure()`, and every result carries the `scan` block that window produced. Measurement is
not instrumentation bolted on afterwards — it is in the return type.

---

## Level 4 — Code

C4's optional level, and the one that ages fastest. This is the shape of the engine's own types, kept
to what a reader needs in order to follow a call.

```mermaid
classDiagram
    class AppConfig {
        +bool mcp_enabled
        +str mcp_path
        +tuple sources
        +str release
        +from_env() AppConfig
    }
    class EngineDependencies {
        +DatasetScope scope
        +Session session
        +kwargs dict
        +narrowed_to(names) EngineDependencies
    }
    class DatasetScope {
        +str release
        +names list
        +default(release) DatasetScope
        +restricted_to(names, release) DatasetScope
        +narrowed_to(names) DatasetScope
        +get(name) Source
        +target(name) str
        +assert_within(path) str
        +entries() list
    }
    class Source {
        +str name
        +str theme
        +str subtype
        +str bbox_column
        +str geometry_column
        +bool polygonal
        +str root
        +prefix(release) str
        +scan_target(release) str
    }
    class Session {
        +SessionConfig config
        +measure() Measurement
        +require_extension(name)
    }
    class SessionConfig {
        +str memory_limit
        +int threads
        +int http_timeout_seconds
        +bool http_metadata_cache
    }
    class Measurement {
        +ScanReport report
        +execute(sql, params)
        +records(sql, params) list
        +one(sql, params) dict
    }
    class ScanReport {
        +int bytes_scanned
        +int http_requests
        +int remote_files_touched
        +float elapsed_ms
        +as_dict() dict
    }
    class BoundingBox {
        +float min_lon
        +float min_lat
        +float max_lon
        +float max_lat
        +predicate(bbox_column) str
    }

    AppConfig ..> EngineDependencies : resolved into
    EngineDependencies *-- DatasetScope
    EngineDependencies *-- Session
    DatasetScope o-- Source : one per dataset in scope
    Session *-- SessionConfig
    Session ..> Measurement : yields one per window
    Measurement *-- ScanReport
    BoundingBox ..> DatasetScope : predicate applied to Source.bbox_column
```

`BoundingBox.predicate()` is the smallest box on this page and the one the whole project rests on. It
writes four independent comparisons on the `bbox` STRUCT members rather than an `ST_Intersects` over
the geometry, because Parquet keeps min/max statistics per column chunk: a plain comparison is
something the reader can evaluate against a footer, and a spatial function is an opaque call it must
materialise rows to run.

### Anchors

| Element | Where |
| --- | --- |
| Container wiring, both protocols | `src/geoparquet_mcp/app.py:78` |
| The double attachment at `/mcp` | `src/geoparquet_mcp/app.py:122` |
| MCP server construction and registration | `src/geoparquet_mcp/server.py:125` |
| Transport as a bare ASGI app | `src/geoparquet_mcp/server.py:139` |
| The lifespan a mount does not get | `src/geoparquet_mcp/server.py:178` |
| Perimeter resolved once | `src/geoparquet_mcp/dependencies.py:72` |
| What a handler is allowed to use | `src/geoparquet_mcp/dependencies.py:124` |
| Name to path, and nothing else | `src/geoparquet_mcp/engine/sources.py:338` |
| The one path-handling code path | `src/geoparquet_mcp/engine/sources.py:345` |
| Byte accounting per window | `src/geoparquet_mcp/engine/session.py:255` |
| Envelope first, exact second | `src/geoparquet_mcp/engine/operations.py:744` |
| SQL bounded by parsing | `src/geoparquet_mcp/engine/query.py:106` |

---

## Supplementary: one tool call, end to end

C4 calls this a dynamic diagram — the same components, ordered by time. This is
`geoparquet_filter_spatial` with a WKT polygon, which is the call that exercises every boundary at
once.

```mermaid
sequenceDiagram
    autonumber
    participant Agent as MCP client
    participant App as app.py
    participant Srv as server.py
    participant Tool as tools/spatial.py
    participant Deps as dependencies.py
    participant Ops as engine/operations.py
    participant Scope as engine/sources.py
    participant Sess as engine/session.py
    participant Duck as DuckDB
    participant S3 as Overture bucket

    Agent->>App: POST /mcp — tools/call
    App->>Srv: routed to the mounted ASGI app
    Srv->>Tool: filter_spatial(source, wkt, limit)
    Tool->>Deps: engine_kwargs()
    Deps-->>Tool: scope + session installed at startup
    Tool->>Ops: spatial_filter(..., scope=, session=)
    Ops->>Ops: validate with the Pydantic request model
    Ops->>Scope: get(name) and target(name)
    Scope-->>Ops: Source + read_parquet target for the pinned release
    Ops->>Sess: measure() — private cursor, connection id, watermark
    Ops->>Duck: ST_Envelope of the WKT
    Duck-->>Ops: the pruning rectangle
    Ops->>Duck: SELECT ... WHERE bbox.xmin <= ? AND ... AND ST_Intersects(...)
    Duck->>S3: footer reads, then surviving column chunks
    S3-->>Duck: bytes
    Duck-->>Ops: rows
    Ops->>Sess: window closes — sum GETs on this connection after the watermark
    Sess-->>Ops: ScanReport: bytes, requests, files, ms
    Ops-->>Tool: GeoJSON FeatureCollection + scan block
    Tool-->>Srv: dict
    Note over Srv: an EngineError here becomes a ToolError,<br/>so its text reaches the model instead of<br/>"Error executing tool X"
    Srv-->>Agent: tool result
```

Steps 8 and 12 are the exactness contract: the envelope prunes the read and the exact geometry then
filters the survivors, so the answer is exact and the read stays proportional to the envelope rather
than to the dataset.

---

## Supplementary: deployment

Two deployments, and they are different process shapes rather than different configurations.

```mermaid
flowchart TB
    subgraph laptop["Deployment node: developer laptop — macOS or Windows"]
        desktop["<b>Claude Desktop</b><br/><i>[Software system]</i>"]
        subgraph venv["Deployment node: project virtualenv"]
            proc1["<b>geoparquet-mcp-server</b><br/><i>[Container]</i><br/>launched as a subprocess,<br/>MCP over stdio"]
        end
        desktop -->|"stdin/stdout"| proc1
    end

    subgraph host["Deployment node: any host that can run uvicorn"]
        proc2["<b>geoparquet-mcp-http</b><br/><i>[Container]</i><br/>REST + MCP at /mcp.<br/>GEOPARQUET_ENABLE_MCP=0 makes it REST-only:<br/>the sub-application is never built."]
    end

    subgraph aws["Deployment node: AWS us-west-2"]
        bucket["<b>overturemaps-us-west-2</b><br/><i>[External: S3 bucket]</i><br/>anonymous reads"]
    end

    proc1 -->|"[HTTPS range requests]"| bucket
    proc2 -->|"[HTTPS range requests]"| bucket

    classDef container fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    class proc1,proc2 container
    class desktop,bucket external
```

The laptop deployment is the constraint that decided the architecture. Claude Desktop starts a
subprocess and talks to its pipes: no orchestrator, no supervisor, nothing to keep a second process
in step. `docs/claude-desktop.md` is the configuration, and `./scripts/serve.sh config` prints it
with this clone's absolute path already in place; `./scripts/serve.sh stdio` and `./scripts/serve.sh
http` start the two containers above. Both entry points are driven over their own pipes by
`tests/test_cli.py`, because the step that makes a served process legitimate — resolving the
perimeter and installing it before serving — is a line whose absence no diagram and no AST walk can
see.

Environment variables are the only deployment-time knobs: `GEOPARQUET_SOURCES` narrows the perimeter,
`GEOPARQUET_RELEASE` pins a release, `GEOPARQUET_ENABLE_MCP` decides whether the MCP sub-application
exists at all, `GEOPARQUET_MCP_PATH` moves it, and `GEOPARQUET_MCP_ALLOWED_HOSTS` extends the SDK's
DNS-rebinding protection beyond localhost.

---

## What keeps these diagrams honest

A C4 diagram is a claim about which arrows exist, and a claim like that rots quietly. Most of the
ones above are enforced by a test rather than by review:

| The diagram says | The test that fails otherwise |
| --- | --- |
| Only `server.py` touches the protocol; the engine imports in a process with no `mcp` | `tests/test_layering.py` |
| Every arrow from `tools/` into `engine/` is one call, with nothing on either side of it | `tests/test_layering.py` — each handler's AST must be a single `return` |
| No SQL and no DuckDB anywhere under `tools/`, including in the prose a model reads | `tests/test_layering.py` |
| `sources.py` is the only name-to-path boundary, and it cannot be walked around | `tests/test_scope_isolation.py` — 18 attempts to escape the perimeter |
| The MCP surface a model sees is exactly what is drawn here | `tests/snapshots/mcp_surface.json` via `tests/test_protocol_surface.py` |
| The chained MCP lifespan is really chained, and `/mcp` answers without a redirect | `tests/test_app.py` |
| `cli.py` names only what the engine exports and never reaches through `tools/` | `tests/test_cli.py`, which walks its syntax tree |

When a diagram here and a test disagree, the test is right. Update the picture.
