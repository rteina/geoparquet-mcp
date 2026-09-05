"""The FastAPI application, with the MCP server mounted inside it.

One process, two protocols, one implementation. The MCP server is not a
separate service behind a proxy and not a sidecar: `server.mcp_asgi_app()`
returns the streamable-HTTP transport as an ASGI application, so it mounts
into FastAPI the way any sub-application does and shares this process's
memory, its DuckDB session and its lifetime.

That sharing is the point. The REST routes below and the MCP tools call the
same engine functions with the same injected perimeter. Neither reimplements
the other, and the pair is the demonstration: a protocol is a façade, and a
capability that has one façade can have two for the cost of the wiring in this
file.

The lifespan does the work that makes the claim true:

  * the dataset perimeter is resolved once, here, and installed — so no
    handler, on either protocol, decides for itself what it may read;
  * the MCP session manager's own lifespan is chained into this one, because a
    mounted ASGI application gets no startup event from the parent router.
    Forget that and every request to the endpoint fails on a session manager
    that was never started.

When MCP is switched off the sub-application is never built and the route is
never registered. `/mcp` then returns FastAPI's own 404 because nothing is
there — not because a handler decided to refuse.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from starlette.routing import Route

from geoparquet_mcp import __version__, dependencies, engine, server
from geoparquet_mcp.config import AppConfig
from geoparquet_mcp.dependencies import EngineDependencies
from geoparquet_mcp.engine.errors import EngineError, InvalidRequestError, UnknownSourceError

logger = logging.getLogger(__name__)

DESCRIPTION = """\
Spatial analysis over remote GeoParquet files, read in place by DuckDB.

Nothing is downloaded or imported first: every query issues HTTP range
requests against public object storage and reads only the row groups and
columns its filters need. Each response reports the bytes that actually
crossed the network.

The same operations are served over MCP at the mount point reported by
`/health`. These REST routes and those MCP tools call the same engine
functions in the same process — they are two façades over one implementation,
not two implementations.
"""

# Engine failures are the caller's fault or the network's, never a bug to leak
# as a 500. Each maps to the status code that tells the caller what to change.
_STATUS_BY_ERROR = {InvalidRequestError: 400, UnknownSourceError: 404}


def engine_dependencies() -> EngineDependencies:
    """FastAPI dependency: the perimeter this request may read.

    The REST side of the same injection the MCP handlers get from
    `dependencies.current()`. Both end up holding the identical object.
    """
    return dependencies.current()


Deps = Depends(engine_dependencies)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the application. The only place the two protocols are wired together."""
    config = config or AppConfig.from_env()
    mcp_server = server.build_server() if config.mcp_enabled else None
    mcp_app = (
        None
        if mcp_server is None
        else server.mcp_asgi_app(mcp_server, allowed_hosts=config.allowed_hosts)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = dependencies.resolve(config)
        dependencies.install(resolved)
        logger.info(
            "geoparquet-mcp ready: release=%s sources=%s mcp=%s",
            resolved.scope.release,
            ",".join(resolved.scope.names),
            config.mcp_path if config.mcp_enabled else "disabled",
        )
        async with AsyncExitStack() as stack:
            if mcp_server is not None:
                # A mounted ASGI app has no lifespan of its own that the
                # parent router will run. The streamable-HTTP transport needs
                # its session manager started, so it is chained here.
                await stack.enter_async_context(server.mcp_lifespan(mcp_server))
            yield
        dependencies.clear()

    app = FastAPI(
        title="geoparquet-mcp",
        summary="Spatial analysis over remote GeoParquet, as REST and as MCP.",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.state.config = config
    _register_error_handlers(app)
    _register_routes(app, config)
    if mcp_app is not None:
        _mount_mcp(app, mcp_app, config.mcp_path)
    return app


def _mount_mcp(app: FastAPI, mcp_app: Any, path: str) -> None:
    """Attach the MCP ASGI application at `path`, and at `path` exactly.

    `Mount` alone is not enough, and the reason is structural rather than a
    detail worth hiding: Starlette compiles a mount to `^/mcp/(?P<path>.*)$`,
    which needs a segment after the prefix. A bare `POST /mcp` therefore
    matches nothing, and the router's trailing-slash recovery answers it with
    a 307 to `/mcp/`. Streamable HTTP is a single endpoint, not a tree, so
    that redirect is pure friction on every request a client makes.

    So the same ASGI application is attached twice: a `Route` for the exact
    path, which is the endpoint clients are given, and a `Mount` for anything
    below it. One object, mounted as a sub-application, reachable at the
    address `/health` advertises.
    """
    app.router.routes.append(Route(path, mcp_app, name="mcp"))
    app.mount(path, mcp_app, name="mcp_subtree")


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(EngineError)
    async def _engine_error(_request: Any, exc: EngineError) -> JSONResponse:
        status = next(
            (code for kind, code in _STATUS_BY_ERROR.items() if isinstance(exc, kind)), 502
        )
        return JSONResponse(status_code=status, content={"error": type(exc).__name__,
                                                         "detail": str(exc)})


def _register_routes(app: FastAPI, config: AppConfig) -> None:
    """The REST façade: the same operations, over the other protocol."""

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        """Whether the process is up, and whether MCP is mounted in it.

        Reports the mount point rather than a bare boolean so a client can
        find the MCP endpoint without being told where it is, and reports
        `null` when MCP is off — because then there is no endpoint, not an
        endpoint that refuses.
        """
        resolved = dependencies.installed()
        return {
            "status": "ok",
            "version": __version__,
            "mcp_enabled": config.mcp_enabled,
            "mcp_mounted_at": config.mcp_path if config.mcp_enabled else None,
            "mcp_transport": "streamable-http" if config.mcp_enabled else None,
            "perimeter": resolved.as_dict() if resolved else None,
        }

    @app.get("/sources", tags=["discovery"])
    def sources(deps: EngineDependencies = Deps) -> dict[str, Any]:
        """The datasets in scope. The REST form of the `geoparquet://sources` resource."""
        return {
            "release": deps.scope.release,
            "default_source": engine.DEFAULT_SOURCE,
            "sources": deps.scope.entries(),
        }

    @app.get("/sources/{source}/schema", tags=["discovery"])
    def schema(source: str, deps: EngineDependencies = Deps) -> dict[str, Any]:
        """One dataset's schema, CRS and extent. Mirrors `geoparquet_describe_source`."""
        return engine.dataset_schema(source=source, **deps.kwargs)

    @app.get("/sources/{source}/preview", tags=["discovery"])
    def preview(
        source: str,
        limit: int = Query(default=10, ge=1, le=100),
        deps: EngineDependencies = Deps,
    ) -> dict[str, Any]:
        """The first rows of a dataset. Mirrors `geoparquet_preview_rows`."""
        return engine.preview_rows(source=source, limit=limit, **deps.kwargs)

    @app.get("/query/spatial", tags=["spatial"])
    def spatial(
        source: str = engine.DEFAULT_SOURCE,
        min_lon: float | None = None,
        min_lat: float | None = None,
        max_lon: float | None = None,
        max_lat: float | None = None,
        wkt: str | None = None,
        category: str | None = None,
        name_contains: str | None = None,
        min_confidence: float | None = None,
        include_geometry: bool = True,
        limit: int = Query(default=50, ge=1),
        deps: EngineDependencies = Deps,
    ) -> dict[str, Any]:
        """Features inside a rectangle or WKT geometry. Mirrors `geoparquet_filter_spatial`."""
        return engine.spatial_filter(
            source=source, min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
            wkt=wkt, category=category, name_contains=name_contains,
            min_confidence=min_confidence, include_geometry=include_geometry, limit=limit,
            **deps.kwargs,
        )

    @app.get("/query/nearest", tags=["spatial"])
    def nearest(
        lon: float,
        lat: float,
        radius_km: float = 1.0,
        source: str = engine.DEFAULT_SOURCE,
        category: str | None = None,
        limit: int = Query(default=20, ge=1),
        deps: EngineDependencies = Deps,
    ) -> dict[str, Any]:
        """Closest features to a point. Mirrors `geoparquet_find_nearest`."""
        return engine.nearest(
            lon=lon, lat=lat, radius_km=radius_km, source=source, category=category,
            limit=limit, **deps.kwargs,
        )

    @app.get("/query/aggregate", tags=["spatial"])
    def aggregate(
        group_by: str,
        source: str = engine.DEFAULT_SOURCE,
        aggregate: str = "count",
        measure: str | None = None,
        min_lon: float | None = None,
        min_lat: float | None = None,
        max_lon: float | None = None,
        max_lat: float | None = None,
        limit: int = Query(default=50, ge=1),
        deps: EngineDependencies = Deps,
    ) -> dict[str, Any]:
        """Grouped aggregate over a dataset. Mirrors `geoparquet_aggregate_attribute`."""
        return engine.attribute_aggregate(
            group_by=group_by, source=source, aggregate=aggregate, measure=measure,
            min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
            limit=limit, **deps.kwargs,
        )

    @app.get("/query/h3", tags=["spatial"])
    def h3(
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        resolution: int = Query(default=8, ge=0, le=15),
        source: str = engine.DEFAULT_SOURCE,
        limit: int = Query(default=200, ge=1),
        deps: EngineDependencies = Deps,
    ) -> dict[str, Any]:
        """H3 density bins over a rectangle. Mirrors `geoparquet_summarize_h3`."""
        return engine.h3_aggregate(
            min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
            resolution=resolution, source=source, limit=limit, **deps.kwargs,
        )

    @app.get("/query/in-polygons", tags=["spatial"])
    def in_polygons(
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        point_source: str = engine.DEFAULT_SOURCE,
        polygon_source: str = "overture_divisions",
        polygon_subtype: str | None = None,
        limit: int = Query(default=50, ge=1),
        deps: EngineDependencies = Deps,
    ) -> dict[str, Any]:
        """Point-in-polygon counts. Mirrors `geoparquet_count_in_polygons`."""
        return engine.point_in_polygon(
            min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
            point_source=point_source, polygon_source=polygon_source,
            polygon_subtype=polygon_subtype, limit=limit, **deps.kwargs,
        )


    if not config.mcp_enabled:
        logger.info("MCP is disabled; no sub-application was mounted")


def main() -> None:
    """Run the combined application under uvicorn."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="geoparquet-mcp-http",
        description="FastAPI application with the MCP server mounted inside it.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=args.host, port=args.port)


# For `uvicorn geoparquet_mcp.app:create_app --factory`, which builds the app
# after the environment is set rather than at import time.

if __name__ == "__main__":
    main()
