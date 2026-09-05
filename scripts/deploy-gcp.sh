#!/usr/bin/env bash

# geoparquet-mcp deploy-gcp — run this server on Google Cloud Run
# Usage: ./scripts/deploy-gcp.sh [command] [options]
#   command: deploy | url | config | logs | delete | help    (default: deploy)
#
#   deploy    Build the image from source with Cloud Build and deploy it as a
#             Cloud Run service. Idempotent: the same command creates the
#             service the first time and updates it every time after.
#   url       Print the service URL. The MCP endpoint is that URL plus /mcp.
#   config    Print the MCP client configuration for the deployed service.
#   logs      Tail the service logs.
#   delete    Delete the service. Nothing else is created, so this is the whole
#             teardown.
#   help      Show this text.
#
# Options:
#   --dry-run          Print the gcloud command that would run, and stop.
#   --project <id>     GCP project (default: $GCP_PROJECT, else gcloud's current).
#   --region <name>    Cloud Run region (default: $GCP_REGION, else europe-west1).
#   --service <name>   Service name (default: $GCP_SERVICE, else geoparquet-mcp).
#   --public           Deploy with --allow-unauthenticated. Off by default: an
#                      open endpoint is your egress bill, paid for whoever finds it.
#   --warm             Keep one instance alive, so the DuckDB footer cache
#                      survives between queries. Costs hundreds of dollars a
#                      month; the default scales to zero and pays ~10 s on the
#                      first query after an idle period instead.
#   --host <fqdn>      Hostname the MCP transport accepts, for a custom domain.
#                      Not needed for the run.app hostname: the deploy reads it
#                      back from the service and sets it.
#
# What the deployed process may read is decided by the environment, the same way
# it is locally. These are read from your shell and passed to the service:
#   GEOPARQUET_SOURCES   narrow the perimeter, e.g. overture_places
#   GEOPARQUET_RELEASE   pin an Overture release, skipping the bucket listing
#                        every cold start would otherwise do before serving
#
# The resource flags below are not defaults worth changing casually; see
# docs/deployment-gcp.md for why each is what it is.
#
# Examples:
#   ./scripts/deploy-gcp.sh deploy --dry-run
#   GEOPARQUET_RELEASE=2026-08-19.0 ./scripts/deploy-gcp.sh deploy
#   ./scripts/deploy-gcp.sh url
#   ./scripts/deploy-gcp.sh config

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_common.sh"

usage() { usage_from_header "$0"; }

# -------------------------------------------------------------------
# Settings
# -------------------------------------------------------------------
REGION="${GCP_REGION:-europe-west1}"
SERVICE="${GCP_SERVICE:-geoparquet-mcp}"
PROJECT="${GCP_PROJECT:-}"
DRY_RUN=0
PUBLIC=0

# The Host header the MCP transport will accept, on top of localhost. Without
# it every request to /mcp is answered 421 Misdirected Request: the SDK turns on
# DNS-rebinding protection by default and its allow-list holds 127.0.0.1 alone,
# while Cloud Run sends the service's own hostname. Empty here means "work it
# out after the deploy", which is the only way round the fact that the hostname
# is assigned by a service that does not exist yet.
ALLOWED_HOST="${GCP_ALLOWED_HOST:-}"

# The shape of the instance, and the reasoning behind it in one line each.
# docs/deployment-gcp.md has the long form.
CPU="${GCP_CPU:-4}"                  # SessionConfig runs DuckDB with threads=4.
MEMORY="${GCP_MEMORY:-4Gi}"          # memory_limit is 2GB; leave DuckDB room above it.
CONCURRENCY="${GCP_CONCURRENCY:-8}"  # One process, one DuckDB session, four threads.
MAX_INSTANCES="${GCP_MAX_INSTANCES:-3}"  # A ceiling on what a bad afternoon can cost.
TIMEOUT="${GCP_TIMEOUT:-600}"        # Longer than any single tool call should take.

# Scale to zero by default, and pay for the seconds a request is actually in
# flight. The cost of that choice is the footer cache: `Session` is a
# process-wide singleton whose ~26 MB of cached Parquet footers is what makes
# the second query cheap, and an instance that shuts down takes it with it. So
# the first query after an idle period pays for the footers again — around ten
# seconds — and every one after that is fast until the instance goes away.
#
# --warm buys that back by keeping one instance alive. It is not a small
# upgrade: a warm instance is billed for every second of the month whether or
# not anyone calls it, which for 4 vCPU and 4 GiB is a bill in the hundreds of
# dollars rather than the tens. Turn it on for something people depend on, not
# for something you show to three people a month.
MIN_INSTANCES="${GCP_MIN_INSTANCES:-0}"
WARM=0

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

# Print a command the way someone would write it: the verb on the first line,
# then one flag per line with its value beside it. This is what --dry-run
# exists for, so the output has to be readable and paste-able, not a token dump.
show_command() {
    local out="$1" token value
    shift
    while [ $# -gt 0 ]; do
        token="$1"
        shift
        case "$token" in
            -*)
                # A flag carries the next token with it, unless that token is
                # itself a flag (--no-cpu-throttling takes no value).
                if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
                    value="$1"
                    shift
                    out+=" \\"$'\n'"    $token $(quoted "$value")"
                else
                    out+=" \\"$'\n'"    $token"
                fi
                ;;
            # Positionals before the first flag stay on the first line:
            # `gcloud run deploy geoparquet-mcp` is one thought.
            *) out+=" $(quoted "$token")" ;;
        esac
    done
    printf '%s\n' "$out" >&2
}

# Shell-quote a value only when it needs it, so the common case stays legible.
quoted() {
    case "$1" in
        *[!A-Za-z0-9._:/=,@+-]*) printf "'%s'" "$1" ;;
        *) printf '%s' "$1" ;;
    esac
}

# Run a command, or print it, depending on --dry-run.
run_or_show() {
    if [ "$DRY_RUN" -eq 1 ]; then
        note "--dry-run: not executing."
        show_command "$@"
        return 0
    fi
    "$@"
}

require_gcloud() {
    # --dry-run is for reading the command before anyone runs it, so it must
    # work on a machine with no CLI and no configured project. Everything else
    # needs both, and says so rather than failing inside gcloud.
    if [ "$DRY_RUN" -eq 1 ]; then
        [ -n "$PROJECT" ] || PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
        case "$PROJECT" in
            ""|"(unset)") PROJECT="<your-project>"; warn "no project configured; using a placeholder." ;;
        esac
        return 0
    fi
    require_tool gcloud "Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install"
    if [ -z "$PROJECT" ]; then
        PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
        [ -n "$PROJECT" ] && [ "$PROJECT" != "(unset)" ] || fail \
            "no GCP project. Pass --project <id>, set GCP_PROJECT, or run: gcloud config set project <id>"
    fi
}

# The environment variables the service runs under, as one --set-env-vars value.
# Only what is set locally is forwarded; an unset variable means the default the
# application already documents, not an empty string overriding it.
service_env() {
    local pairs=()
    [ -n "${GEOPARQUET_SOURCES:-}" ] && pairs+=("GEOPARQUET_SOURCES=$GEOPARQUET_SOURCES")
    [ -n "${GEOPARQUET_RELEASE:-}" ] && pairs+=("GEOPARQUET_RELEASE=$GEOPARQUET_RELEASE")
    [ -n "${GEOPARQUET_ENABLE_MCP:-}" ] && pairs+=("GEOPARQUET_ENABLE_MCP=$GEOPARQUET_ENABLE_MCP")
    [ -n "${GEOPARQUET_MCP_PATH:-}" ] && pairs+=("GEOPARQUET_MCP_PATH=$GEOPARQUET_MCP_PATH")
    # GEOPARQUET_MCP_ALLOWED_HOSTS is not read from the shell: it has to name
    # the hostname Cloud Run assigns, which is not known until the service
    # exists. `reconcile_allowed_hosts` sets it after the first deploy.
    [ -n "$ALLOWED_HOST" ] && pairs+=("GEOPARQUET_MCP_ALLOWED_HOSTS=$ALLOWED_HOST")
    if [ ${#pairs[@]} -gt 0 ]; then
        (IFS=,; echo "${pairs[*]}")
    fi
    return 0
}

service_url() {
    require_gcloud
    gcloud run services describe "$SERVICE" \
        --project "$PROJECT" --region "$REGION" \
        --format 'value(status.url)' 2>/dev/null \
        || fail "service '$SERVICE' not found in $REGION. Deploy it first: $0 deploy"
}

# The service URL if the service exists, and nothing if it does not. Used before
# a deploy, where "not there yet" is the normal case and not a failure.
existing_url() {
    gcloud run services describe "$SERVICE" \
        --project "$PROJECT" --region "$REGION" \
        --format 'value(status.url)' 2>/dev/null || true
}

# The bare hostname of a URL: what a client puts in the Host header, and so what
# the MCP transport has to be told to accept.
host_of() {
    local host="${1#https://}"
    host="${host#http://}"
    printf '%s' "${host%%/*}"
}

# -------------------------------------------------------------------
# Commands
# -------------------------------------------------------------------

do_deploy() {
    require_gcloud

    local auth_flag="--no-allow-unauthenticated"
    [ "$PUBLIC" -eq 1 ] && auth_flag="--allow-unauthenticated"

    local args=(
        run deploy "$SERVICE"
        --source "$PROJECT_DIR"
        --project "$PROJECT"
        --region "$REGION"
        --cpu "$CPU"
        --memory "$MEMORY"
        --concurrency "$CONCURRENCY"
        --min-instances "$MIN_INSTANCES"
        --max-instances "$MAX_INSTANCES"
        --timeout "$TIMEOUT"
        --port 8080
        "$auth_flag"
    )

    # Always-allocated CPU goes with a warm instance and not otherwise: it is
    # what lets the MCP session manager keep working between requests, and it
    # is also what switches Cloud Run to billing the instance's whole lifetime.
    # This server has nothing to do between requests — it pushes no
    # notifications and holds no subscriptions — so throttled CPU costs it
    # nothing but idle time nobody pays for.
    [ "$WARM" -eq 1 ] && args+=(--no-cpu-throttling)

    local env_pairs
    env_pairs="$(service_env)"
    [ -n "$env_pairs" ] && args+=(--set-env-vars "$env_pairs")

    # Cloud Run assigns the hostname, so the allow-list can only be filled in
    # from a service that already exists. On a redeploy that is this one, and
    # the value goes in with everything else; on the very first deploy there is
    # nothing to read, and `reconcile_allowed_hosts` adds it afterwards.
    if [ -z "$ALLOWED_HOST" ] && [ "$DRY_RUN" -eq 0 ]; then
        ALLOWED_HOST="$(host_of "$(existing_url)")"
    fi

    step "Deploying $SERVICE to $REGION (project $PROJECT)"
    [ "$PUBLIC" -eq 1 ] && warn "--public: this endpoint will be reachable by anyone who finds it."
    [ "$WARM" -eq 1 ] && warn "--warm: one instance stays alive and is billed for every second of the month."
    [ -z "${GEOPARQUET_RELEASE:-}" ] && note \
        "GEOPARQUET_RELEASE is unset: every cold start will list the Overture bucket before serving."
    [ -z "${GEOPARQUET_SOURCES:-}" ] && note \
        "GEOPARQUET_SOURCES is unset: the service will expose every registered dataset."

    run_or_show gcloud "${args[@]}"
    [ "$DRY_RUN" -eq 1 ] && return 0

    local url
    url="$(service_url)"
    reconcile_allowed_hosts "$url"
    ok "Deployed."
    note "MCP    POST $url/mcp"
    note "REST   GET  $url/health, $url/sources, $url/query/spatial"
    note "Client configuration: $0 config"
}

# Teach the MCP transport the hostname it was just given.
#
# This is a second revision, and only ever on the first deploy: without it every
# POST to /mcp is answered 421 Misdirected Request. The SDK enables DNS-rebinding
# protection by default with 127.0.0.1 as the whole allow-list, and Cloud Run
# sends the service's own hostname in the Host header. The REST routes are
# unaffected — the check belongs to the MCP transport, not to FastAPI — so a
# service in this state looks healthy and answers /sources while speaking no MCP
# at all.
reconcile_allowed_hosts() {
    local host
    host="$(host_of "$1")"
    [ -n "$host" ] || return 0
    [ "$host" = "$ALLOWED_HOST" ] && return 0

    step "Allowing the MCP transport to answer on $host"
    note "One extra revision, on the first deploy only: the hostname did not"
    note "exist when the service was created."
    gcloud run services update "$SERVICE" \
        --project "$PROJECT" --region "$REGION" \
        --update-env-vars "GEOPARQUET_MCP_ALLOWED_HOSTS=$host" \
        --quiet >/dev/null
    ALLOWED_HOST="$host"
}

do_config() {
    require_gcloud
    local url
    url="$(service_url)"

    note "The service speaks MCP over streamable HTTP at the endpoint below."
    note "A private service needs a proxy in front of it, because no MCP client"
    note "signs a Google identity token:"
    note "  gcloud run services proxy $SERVICE --project $PROJECT --region $REGION --port 8080"
    note "then point the client at http://localhost:8080/mcp instead."

    cat <<JSON
{
  "mcpServers": {
    "geoparquet": {
      "type": "http",
      "url": "$url/mcp"
    }
  }
}
JSON
}

do_logs() {
    require_gcloud
    step "Tailing logs for $SERVICE. Ctrl+C to stop."
    local group=()
    gcloud run services logs tail --help >/dev/null 2>&1 || group=(beta)
    exec gcloud "${group[@]}" run services logs tail "$SERVICE" \
        --project "$PROJECT" --region "$REGION"
}

do_delete() {
    require_gcloud
    step "Deleting $SERVICE from $REGION (project $PROJECT)"
    run_or_show gcloud run services delete "$SERVICE" \
        --project "$PROJECT" --region "$REGION" --quiet
}

# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------
main() {
    local cmd="deploy"
    case "${1:-}" in
        deploy|url|config|logs|delete) cmd="$1"; shift ;;
        -h|--help|help) usage; return 0 ;;
        "") ;;
        --*) ;;  # options with no command: they belong to the default one
        *)
            echo "Error: unknown command '$1' (expected: deploy, url, config, logs, delete, help)" >&2
            usage >&2
            exit 1
            ;;
    esac

    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)  DRY_RUN=1; shift ;;
            --public)   PUBLIC=1; shift ;;
            --warm)     WARM=1; MIN_INSTANCES=1; shift ;;
            --host)     ALLOWED_HOST="${2:?--host needs a value}"; shift 2 ;;
            --project)  PROJECT="${2:?--project needs a value}"; shift 2 ;;
            --region)   REGION="${2:?--region needs a value}"; shift 2 ;;
            --service)  SERVICE="${2:?--service needs a value}"; shift 2 ;;
            -h|--help)  usage; return 0 ;;
            *)
                echo "Error: unknown option '$1'" >&2
                usage >&2
                exit 1
                ;;
        esac
    done

    case "$cmd" in
        deploy) do_deploy ;;
        url)    service_url ;;
        config) do_config ;;
        logs)   do_logs ;;
        delete) do_delete ;;
    esac
}

main "$@"
