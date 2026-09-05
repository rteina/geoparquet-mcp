#!/usr/bin/env bash

# Shared helpers for the geoparquet-mcp scripts — sourced, never executed.
#
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$SCRIPT_DIR/_common.sh"
#
# Holds the project paths, the output helpers, and the environment bootstrap.
# The bootstrap is the reason these scripts exist at all: a fresh clone has to
# reach a working demo in one command, on a machine that may or may not have uv
# installed and may or may not have a recent enough Python.
#
# Progress messages go to STDERR on purpose. A script's stdout may be the data
# itself (`demo.sh --json`), and bootstrap chatter must never end up inside it.

# Guard against double-sourcing.
[ -n "${GEOPARQUET_MCP_COMMON_SH:-}" ] && return 0
GEOPARQUET_MCP_COMMON_SH=1

# -------------------------------------------------------------------
# Paths
# -------------------------------------------------------------------
_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$_COMMON_DIR"
PROJECT_DIR="$(cd "$_COMMON_DIR/.." && pwd)"
VENV_DIR="${UV_PROJECT_ENVIRONMENT:-$PROJECT_DIR/.venv}"

# The console script every command ends up calling.
CLI_BIN="$VENV_DIR/bin/geoparquet-mcp"

# Lowest Python the project supports; keep in step with pyproject.toml.
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=12

# -------------------------------------------------------------------
# Output helpers
# -------------------------------------------------------------------
if [ -t 2 ]; then
    c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_red=$'\033[1;31m'
    c_yellow=$'\033[1;33m'; c_bold=$'\033[1m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
else
    c_blue=''; c_green=''; c_red=''; c_yellow=''; c_bold=''; c_dim=''; c_off=''
fi

step()    { printf '\n%s==>%s %s%s%s\n' "$c_blue" "$c_off" "$c_bold" "$*" "$c_off" >&2; }
section() { printf '\n%s══════ %s ══════%s\n' "$c_bold" "$*" "$c_off" >&2; }
ok()      { printf '  %s[OK]%s   %s\n' "$c_green" "$c_off" "$*" >&2; }
warn()    { printf '  %s[WARN]%s %s\n' "$c_yellow" "$c_off" "$*" >&2; }
note()    { printf '  %s%s%s\n' "$c_dim" "$*" "$c_off" >&2; }
fail()    { printf '\n  %s[FAIL]%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

# Print a script's own header comment block as its help text — derived from the
# source, so `--help` can never drift from the documentation above it.
# Skips the shebang, stops at the first non-comment line.
usage_from_header() {
    awk 'NR < 3 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$1"
}

require_tool() {
    command -v "$1" >/dev/null 2>&1 || fail "$1 not found on PATH. ${2:-}"
}

# -------------------------------------------------------------------
# Environment bootstrap
# -------------------------------------------------------------------

# First interpreter on PATH new enough to run the project. uv can fetch its own,
# so this only matters on the pip fallback path.
find_python() {
    local candidate
    for candidate in python3.14 python3.13 python3.12 python3 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PYTHON_MAJOR, $MIN_PYTHON_MINOR) else 1)" 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# True when the environment is present and no newer manifest has landed since
# the last install. The stamp lives inside the venv, so deleting .venv is always
# enough to force a clean rebuild.
_env_is_current() {
    local stamp="$VENV_DIR/.deps_installed"
    [ -x "$CLI_BIN" ] || return 1
    [ -f "$stamp" ] || return 1
    [ "$PROJECT_DIR/pyproject.toml" -nt "$stamp" ] && return 1
    [ -f "$PROJECT_DIR/uv.lock" ] && [ "$PROJECT_DIR/uv.lock" -nt "$stamp" ] && return 1
    return 0
}

# Create and populate .venv if needed. Idempotent, and cheap once warm.
#
# uv is preferred: it resolves from the committed uv.lock and will download a
# suitable Python if the machine has none. Without uv the script falls back to
# `python3 -m venv` plus an editable pip install, which needs a local Python
# 3.12+ but no other tooling.
ensure_env() {
    _env_is_current && return 0

    if command -v uv >/dev/null 2>&1; then
        note "Syncing dependencies with uv..."
        # --inexact so that running the demo never uninstalls a contributor's
        # dev extras (pytest, ruff) behind their back.
        uv sync --project "$PROJECT_DIR" --inexact --quiet \
            || fail "uv sync failed. Try: rm -rf '$VENV_DIR' && $0"
    else
        local python_bin
        python_bin="$(find_python)" || fail \
            "no Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ found, and uv is not installed. Install uv (https://docs.astral.sh/uv/) or a recent Python."
        note "uv not found — falling back to $python_bin -m venv + pip."
        [ -d "$VENV_DIR" ] || "$python_bin" -m venv "$VENV_DIR" \
            || fail "could not create a virtual environment in $VENV_DIR"
        note "Installing dependencies with pip (this takes a moment)..."
        "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip \
            || fail "could not upgrade pip inside $VENV_DIR"
        "$VENV_DIR/bin/python" -m pip install --quiet --editable "$PROJECT_DIR" \
            || fail "pip install failed. Try: rm -rf '$VENV_DIR' && $0"
    fi

    [ -x "$CLI_BIN" ] || fail "the geoparquet-mcp command is missing from $VENV_DIR after install."
    touch "$VENV_DIR/.deps_installed"
}

# Run the project CLI in the prepared environment, replacing this shell so that
# Ctrl+C reaches the query and the exit status is the CLI's own.
run_cli() {
    ensure_env
    cd "$PROJECT_DIR"
    exec "$CLI_BIN" "$@"
}
