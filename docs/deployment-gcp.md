# Deploying to Google Cloud Run

This server has two transports, and only one of them can be deployed. STDIO is
what a desktop client launches as a subprocess on the user's own machine; it
has no meaning behind a URL. What goes to Cloud Run is the HTTP entry point —
the FastAPI application with the MCP server mounted inside it, one process
answering `POST /mcp` and the REST routes beside it.

Everything needed is in the repository: a `Dockerfile`, a `.dockerignore`, and
`scripts/deploy-gcp.sh`, which wraps the one `gcloud` command that does the
work.

## What has been verified, and what has not

The image was built and run locally. It starts, resolves the Overture release
over the network, reports its perimeter on `/health`, answers a real query
against the public bucket, and speaks MCP on `/mcp`. The `421` described below
was found that way rather than reasoned about, and so was the fix.

**Nothing here has been deployed to GCP.** The `gcloud` commands have never
run against a real project. `./scripts/deploy-gcp.sh deploy --dry-run` prints
them without executing, which is the honest way to read this document.

## Quick start

```sh
gcloud auth login
gcloud config set project <your-project>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
                       artifactregistry.googleapis.com

./scripts/deploy-gcp.sh deploy --dry-run     # read the command first
GEOPARQUET_RELEASE=2026-08-19.0 GEOPARQUET_SOURCES=overture_places \
  ./scripts/deploy-gcp.sh deploy
./scripts/deploy-gcp.sh config               # the MCP client block
```

`deploy` uploads the source, builds the `Dockerfile` with Cloud Build, and
creates or updates the service. It is idempotent: the same command is the
first deploy and every one after it.

Region defaults to `europe-west1`. See [Where to run it](#where-to-run-it) for
what that costs in latency, because the data is not in Europe.

## The two things in the Dockerfile that are not boilerplate

**The DuckDB extensions are installed at build time.** `Session._open()` runs
`INSTALL httpfs` and `INSTALL spatial` on the first connection, and `INSTALL`
fetches a binary from `extensions.duckdb.org`. Left to runtime, every instance
downloads them before it can answer anything, and an instance that cannot
reach that host answers nothing at all. `h3` is installed the same way but
tolerated if it fails: `Session.require_extension` degrades one operation
rather than the process, so a community repository having a bad day should
cost seven working tools, not a broken build.

**`HOME` is set before the install and kept afterwards.** DuckDB resolves its
extension directory from `HOME`. A container that installs as `root` and runs
as `app` finds an empty directory and downloads everything again — the exact
failure the build-time install was meant to prevent, silently reintroduced.

The rest is ordinary: `uv sync --frozen` against the committed lock file, the
dependency layer separate from the source layer, a non-root user, and a bind
on `0.0.0.0:$PORT` because the entry point's own default of `127.0.0.1:8000`
is unreachable from outside a container.

## The 421 that makes MCP look broken

This is the one failure worth reading before deploying, because the service
looks healthy while it happens.

Out of the box, `POST /mcp` behind Cloud Run is answered:

```
HTTP/1.1 421 Misdirected Request
Invalid Host header
```

The MCP SDK enables DNS-rebinding protection by default, and the allow-list it
builds by default holds `127.0.0.1` and `localhost` alone. Cloud Run sends the
service's own hostname in the `Host` header, which is not on that list. The
REST routes are unaffected — the check belongs to the MCP transport, not to
FastAPI — so `/health` returns `200`, `/sources` returns data, and only the
protocol the service exists to speak is refused.

The fix is `GEOPARQUET_MCP_ALLOWED_HOSTS`, naming the hostname Cloud Run
assigned:

```sh
gcloud run services update geoparquet-mcp --region europe-west1 \
  --update-env-vars GEOPARQUET_MCP_ALLOWED_HOSTS=geoparquet-mcp-1234.europe-west1.run.app
```

`scripts/deploy-gcp.sh` does this for you, and the way it does it is a
chicken-and-egg worth knowing about: the hostname does not exist until the
service does. So the first deploy creates the service, reads its URL back, and
sets the variable in a second revision. Every deploy after that reads the
hostname *before* deploying and passes it in with everything else, so the
second revision happens once, ever.

### One limitation this leaves

With the allow-list set, a request carrying an `Origin` header of
`https://<host>` is answered `403 Invalid Origin header`. `server.mcp_asgi_app()`
derives allowed origins from the allowed hosts as `http://{host}` only, and
Cloud Run serves HTTPS.

In practice this affects nothing today: MCP clients that are not browsers —
Claude Desktop, Claude Code, MCP Inspector's CLI, `mcp-remote` — send no
`Origin` header, and the check passes when the header is absent. A
browser-hosted client would be refused. Fixing it is one line in
`mcp_asgi_app`, adding `https://{host}` beside the `http://` form; it has not
been done because nothing in this repository needs it yet.

## The instance, and why it is shaped this way

| Flag | Value | Why |
| --- | --- | --- |
| `--cpu` | 4 | `SessionConfig` runs DuckDB with `threads=4`. Fewer vCPUs make those threads contend rather than work. |
| `--memory` | 4Gi | `memory_limit` is `2GB`, and that is DuckDB's buffer budget, not the process total. 4 GiB is also the minimum Cloud Run allows with 4 vCPU. |
| `--concurrency` | 8 | One process, one DuckDB session, four threads. The default of 80 lets eight simultaneous scans fight over the same buffer pool. |
| `--max-instances` | 3 | A ceiling on what a bad afternoon can cost. |
| `--timeout` | 600 | Longer than any single tool call should take, short enough that a stuck one is not billed for an hour. |
| `--min-instances` | 0 | See below. |

### Scale to zero, or keep it warm

The default scales to zero, and that decision is about the footer cache.

`Session` is a process-wide singleton with DuckDB's HTTP metadata cache on,
and the ~26 MB the first query pays is Parquet footers — the thing that makes
every later query prune row groups instead of reading them. An instance that
shuts down takes that cache with it. So with `--min-instances 0` the first
query after an idle period pays for the footers again, and everything after it
is fast until the instance goes away.

Measured through the container, from a laptop in France:

```
GET /sources/overture_places/schema
  bytes_scanned  26,490,177     (26.49 MB — footers, not data)
  http_requests  36
  elapsed_ms     9,660
```

Ten seconds, once per idle period. That is the price of the default.

`--warm` buys it back by keeping one instance alive and allocating CPU
continuously. It is not a small upgrade: a warm instance is billed for every
second of the month whether or not anyone calls it, and at 4 vCPU / 4 GiB that
is a bill in the hundreds of dollars, not the tens. Scaled to zero, you pay
for the seconds a request is in flight — a few queries a day is cents. Check
the [Cloud Run pricing page](https://cloud.google.com/run/pricing) for the
current numbers; the ratio is the part that will not change.

Use `--warm` for something people depend on. Not for something you show to
three people a month.

## Where to run it

`europe-west1` (Belgium) is the default here, and it is worth being explicit
about the trade-off, because the data is not in Europe.

Overture publishes to `s3://overturemaps-us-west-2`, in Oregon. Every query
this server answers is a series of HTTP range requests against that bucket, and
a good number of them are sequential — read the footer, decide which row groups
survive, fetch those. Latency to the bucket is therefore multiplied by the
number of round trips, not amortised across them. From `us-west1` the round
trip is a few milliseconds; from `europe-west1` it is on the order of 140.

That is a real cost and it falls on every query, not just the first. It buys
two things: proximity to users in Europe for everything that is not the bucket
read, and data residency if that matters to you.

Two mitigations, both already in place. The Parquet footers are cached in the
session, so the round trips that cost the most happen once per instance rather
than once per query — which is an argument for `--warm` that has nothing to do
with convenience. And pinning `GEOPARQUET_RELEASE` removes the bucket listing
that every cold start otherwise does before it can serve.

If latency matters more than location, deploy the same thing to `us-west1`:

```sh
./scripts/deploy-gcp.sh deploy --region us-west1
```

Nothing else changes.

## What the service is allowed to read

The perimeter works exactly as it does locally: `AppConfig.from_env()` reads
it once at startup, and nothing downstream can widen it.

| Variable | Effect |
| --- | --- |
| `GEOPARQUET_SOURCES` | Comma-separated dataset names. Unset means every registered source. |
| `GEOPARQUET_RELEASE` | Pin an Overture release. Unset means list the bucket at startup and resolve the newest. |
| `GEOPARQUET_ENABLE_MCP` | `false` deploys the REST API alone; `/mcp` is then nothing, not a handler that refuses. |
| `GEOPARQUET_MCP_PATH` | Where the MCP sub-application is mounted. |
| `GEOPARQUET_MCP_ALLOWED_HOSTS` | The `Host` values the MCP transport accepts. See above — on Cloud Run this is not optional. |

`scripts/deploy-gcp.sh` forwards the first four from your shell, and manages
the fifth itself.

Pin the release. Not pinning it means every cold start makes a network call to
list the bucket before it can serve a single request, and Overture keeps only
two releases, so the pin is also what stops a query failing halfway through a
month for reasons that have nothing to do with your deployment.

## Authentication

The script deploys `--no-allow-unauthenticated`. That is the right default and
also an awkward one, because no MCP client signs a Google identity token.

**Private, with a proxy.** The simplest thing that works for one person:

```sh
gcloud run services proxy geoparquet-mcp --region europe-west1 --port 8080
```

The client then points at `http://localhost:8080/mcp`, and the proxy signs
every request with your own credentials. Note that this address is on the
localhost allow-list already, so it works whatever `GEOPARQUET_MCP_ALLOWED_HOSTS`
says.

**Public, with `--public`.** Defensible for this particular server: it is
read-only, over public data, bounded by `MAX_ROW_LIMIT` and by whatever
`GEOPARQUET_SOURCES` allows. What you are exposing is not the data — anyone can
read Overture — but your egress bill and your instance hours, to whoever finds
the URL. Narrow the perimeter and keep `--max-instances` low if you do this.

**Anything more.** Identity-Aware Proxy or API Gateway in front of the service
gives real access control, and a bearer-token check would need a middleware the
application does not have. Out of scope here.

## Operating it

```sh
./scripts/deploy-gcp.sh url       # the service URL; MCP is that plus /mcp
./scripts/deploy-gcp.sh config    # the client configuration block
./scripts/deploy-gcp.sh logs      # tail
./scripts/deploy-gcp.sh delete    # the whole teardown; nothing else was created
```

`/health` is the thing to check first, and it answers the question that
matters: which release the process pinned, which datasets are in its perimeter,
and whether MCP is mounted at all.

```json
{
  "status": "ok",
  "version": "0.1.0",
  "mcp_enabled": true,
  "mcp_mounted_at": "/mcp",
  "mcp_transport": "streamable-http",
  "perimeter": { "release": "2026-08-19.0", "sources": ["overture_places"] }
}
```

A service that answers this but returns `421` on `/mcp` has the `Host` problem
described above, and nothing else.

## Building the image by hand

`gcloud run deploy --source` builds on Cloud Build, which is `linux/amd64`, so
the platform takes care of itself. Building locally on an Apple Silicon machine
does not: the image would be `arm64` and Cloud Run would refuse it.

```sh
docker build -t geoparquet-mcp .                       # local checks, native arch
docker run --rm -p 8080:8080 geoparquet-mcp
curl localhost:8080/health

docker build --platform linux/amd64 -t <region>-docker.pkg.dev/<project>/<repo>/geoparquet-mcp .
docker push <region>-docker.pkg.dev/<project>/<repo>/geoparquet-mcp
./scripts/deploy-gcp.sh deploy   # then point --image at it instead of --source
```

The last line is a sketch, not a supported path: the script deploys from source
and has no `--image` flag.

## What this does not cover

Custom domains, Cloud Build triggers on push, VPC egress, Secret Manager, and
IAP. None of them are needed to run this server, and each would be a decision
about your project rather than about this code.
