# geoparquet-mcp as a container image, built for Cloud Run.
#
# The image serves the HTTP entry point — FastAPI with the MCP server mounted
# inside it — because that is the only façade a remote client can reach. The
# stdio transport is still in here, but it is the transport a desktop client
# launches as a subprocess on its own machine; it has no meaning behind a URL.
#
# Two things in this file are not boilerplate, and both are about the cost of a
# cold start.
#
#   * The DuckDB extensions are installed at build time. `Session._open()` runs
#     INSTALL then LOAD for httpfs and spatial on the first connection, and
#     INSTALL pulls a binary from extensions.duckdb.org. Left to runtime, every
#     new instance downloads them before it can answer anything, and an
#     instance that cannot reach that host serves nothing at all.
#   * HOME is set before the extensions are installed and kept afterwards.
#     DuckDB resolves its extension directory from HOME, so a container that
#     installs as root and runs as another user finds an empty directory and
#     downloads them all over again.
#
# Build and run locally:
#   docker build -t geoparquet-mcp .
#   docker run --rm -p 8080:8080 -e PORT=8080 geoparquet-mcp
#   curl localhost:8080/health

FROM python:3.12-slim

# uv, for the same lock file CI resolves from. Pinned to a minor series so a
# rebuild months from now still installs what uv.lock records.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/

ENV HOME=/home/app \
    PATH=/app/.venv/bin:$PATH \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin app
WORKDIR /app

# Dependencies first, project second: the lock file changes far less often than
# the source, so this layer survives most rebuilds.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# hatchling reads both of these while building the wheel; without them the
# second sync fails on the metadata, not on the code.
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# The extensions, baked in. httpfs and spatial are required — the session
# refuses to open without them, so a failure here must fail the build. h3 is
# optional by design: `Session.require_extension` degrades one operation rather
# than the process, so a community repository that is down or unreachable
# leaves seven tools working and must not stop the image being built.
RUN python -c "\
import duckdb; \
con = duckdb.connect(); \
[con.execute(f'INSTALL {name}') or con.execute(f'LOAD {name}') for name in ('httpfs', 'spatial')]; \
print('installed: httpfs, spatial')" \
 && (python -c "\
import duckdb; \
con = duckdb.connect(); \
con.execute('INSTALL h3 FROM community'); \
print('installed: h3')" \
     || echo 'WARNING: h3 not installed; geoparquet_summarize_h3 will report it as unavailable') \
 && chown -R app:app /home/app

USER app

# Cloud Run injects PORT and routes to it; 8080 is its default and the value to
# use when running the image by hand. Binding 0.0.0.0 rather than the entry
# point's own 127.0.0.1 default is what makes the container reachable at all.
ENV PORT=8080
EXPOSE 8080

# Shell form, deliberately: $PORT has to be expanded at run time, and only the
# shell does that. `exec` is what makes it safe — the server replaces the shell
# rather than running under it, so it is PID 1 and Cloud Run's SIGTERM reaches
# the process that has to act on it. Without `exec`, a shell would sit between
# them and swallow the signal, and every revision change would end in a kill.
CMD exec geoparquet-mcp-http --host 0.0.0.0 --port "$PORT"
