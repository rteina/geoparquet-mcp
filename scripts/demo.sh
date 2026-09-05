#!/usr/bin/env bash

# geoparquet-mcp demo — spatial analysis on a remote GeoParquet file
# Usage: ./scripts/demo.sh [command] [options]
#   command: run | sources | help     (default: run)
#
#   run       Query a multi-gigabyte remote dataset four ways and report the
#             bytes that actually crossed the network.
#   sources   Print the catalogue of registered remote datasets as JSON.
#   help      Show this text.
#
# Options for `run`:
#   --source <name>   Registered source to query (default: overture_places).
#   --json            Emit the raw JSON of every step instead of the report.
#
# Nothing is downloaded or imported: DuckDB reads the Parquet in place over HTTP
# range requests, and the bounding box is pushed into the file so only the
# matching row groups are fetched. The last section measures that, by running the
# same query with and without DuckDB's filter pushdown.
#
# The environment is prepared on demand, into .venv/ at the project root. uv is
# used when it is on PATH; otherwise the script falls back to python3 -m venv plus
# pip, which needs a local Python 3.12+.
#
# Expect roughly a minute and ~200 MB of traffic on the first run, most of it the
# deliberately unoptimised comparison query in the last section.
#
# Examples:
#   ./scripts/demo.sh
#   ./scripts/demo.sh run --source overture_buildings
#   ./scripts/demo.sh run --json > demo.json
#   ./scripts/demo.sh sources

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_common.sh"

usage() { usage_from_header "$0"; }

main() {
    local cmd="${1:-run}"
    [ $# -gt 0 ] && shift || true

    case "$cmd" in
        run)             run_cli demo "$@" ;;
        sources)         run_cli sources "$@" ;;
        -h|--help|help)  usage ;;
        # No subcommand given, just options: treat them as options to `run`.
        --*)             run_cli demo "$cmd" "$@" ;;
        *)
            echo "Error: unknown command '$cmd' (expected: run, sources, help)" >&2
            usage >&2
            exit 1
            ;;
    esac
}

main "$@"
