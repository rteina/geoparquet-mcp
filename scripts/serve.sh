#!/usr/bin/env bash

# geoparquet-mcp serve — run the MCP server out of a clone, with no install step
# Usage: ./scripts/serve.sh [command] [options]
#   command: stdio | http | config | help     (default: stdio)
#
#   stdio     Serve MCP over stdin/stdout — the transport a desktop client
#             launches as a subprocess. Run it yourself only to check that it
#             starts: it sits silent, waiting for JSON-RPC on stdin. Ctrl+C exits.
#   http      Serve the FastAPI app with the MCP server mounted inside it:
#               MCP   POST http://HOST:PORT/mcp
#               REST  GET  /sources, /query/spatial, /query/aggregate, /health
#   config    Print the claude_desktop_config.json block for this clone, with
#             the absolute path already filled in.
#   help      Show this text.
#
# Options for `http`:
#   --host <addr>   Interface to bind (default: 127.0.0.1).
#   --port <n>      Port to bind (default: 8000).
#
# The environment is prepared on demand into .venv/, the same way scripts/demo.sh
# does it: uv when it is on PATH, otherwise python3 -m venv plus pip. Bootstrap
# messages go to stderr, so `stdio` keeps stdout as a clean protocol channel.
#
# What the process may read is decided by the environment, once, at startup:
# GEOPARQUET_SOURCES narrows the datasets, GEOPARQUET_RELEASE pins an Overture
# release. See docs/claude-desktop.md for the full list.
#
# Examples:
#   ./scripts/serve.sh
#   ./scripts/serve.sh http --port 9000
#   GEOPARQUET_SOURCES=overture_places ./scripts/serve.sh stdio
#   ./scripts/serve.sh config

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_common.sh"

usage() { usage_from_header "$0"; }

# The block to paste into claude_desktop_config.json. Printed on stdout so it
# can be redirected or piped; everything explaining it goes to stderr.
print_config() {
    ensure_env
    local server_bin="$VENV_DIR/bin/geoparquet-mcp-server"
    [ -x "$server_bin" ] || fail "geoparquet-mcp-server is missing from $VENV_DIR."

    note "Paste this into your Claude Desktop configuration:"
    note "  macOS   ~/Library/Application Support/Claude/claude_desktop_config.json"
    note "  Windows %APPDATA%\\Claude\\claude_desktop_config.json"
    note "Then restart Claude Desktop. The eight tools appear under the connector."

    cat <<JSON
{
  "mcpServers": {
    "geoparquet": {
      "command": "$server_bin",
      "args": ["--transport", "stdio"]
    }
  }
}
JSON
}

main() {
    local cmd="${1:-stdio}"
    [ $# -gt 0 ] && shift || true

    case "$cmd" in
        stdio)
            # Progress goes to stderr and the server replaces this shell, so
            # the first byte on stdout is the first byte of the protocol.
            note "Serving MCP over stdio. Waiting for JSON-RPC on stdin; Ctrl+C to stop."
            run_cli serve --transport stdio "$@"
            ;;
        http)
            note "Serving MCP at POST /mcp and the REST routes beside it."
            run_bin geoparquet-mcp-http "$@"
            ;;
        config)          print_config ;;
        -h|--help|help)  usage ;;
        # No subcommand given, just options: treat them as options to `http`,
        # the only command that takes any.
        --*)             note "Serving MCP at POST /mcp and the REST routes beside it."
                         run_bin geoparquet-mcp-http "$cmd" "$@" ;;
        *)
            echo "Error: unknown command '$cmd' (expected: stdio, http, config, help)" >&2
            usage >&2
            exit 1
            ;;
    esac
}

main "$@"
