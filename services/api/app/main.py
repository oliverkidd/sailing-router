"""FastAPI entrypoint.

API contract:
    POST /boat             -> validated BoatConfig + polar_id
    GET  /models           -> list[ModelPreset]
    GET  /models/available -> models covering a bbox
    GET  /wind/area        -> multi-hour display wind series
    POST /course/prepare   -> prefetch wind + geo for a course
    POST /route            -> RouteResponse (one ModelRoute per model)
    POST /route/stream     -> SSE progress events + final RouteResponse
    GET  /elevation        -> {elevation_m, depth_m, on_land, dem_available}
    GET  /dem/coverage     -> DEM mosaic footprints
    GET  /health
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.types import Receive, Scope, Send

from . import __version__, catalog
from .config import MODEL_PRESETS, model_is_configured

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Ensure app forecast/field logs appear in the API console."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    app_log = logging.getLogger("app")
    app_log.setLevel(level)
    if not app_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        app_log.addHandler(handler)
    # Keep httpx/httpcore quiet — grib_source logs the requests we care about.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logger.info("logging configured level=%s", level_name)


class GZipIgnoreSSE(GZipMiddleware):
    """Don't gzip SSE — buffering defeats live progress events."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).endswith("/route/stream"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    catalog.init_db()
    yield


app = FastAPI(
    title="Sailing Weather Router",
    version=__version__,
    description="Local-first sailing weather router with GRIB wind pipeline.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Compress large /wind/area u/v/speed payloads (typically 5–10× smaller).
# Skip SSE (/route/stream) so stage/leg events flush immediately.
app.add_middleware(GZipIgnoreSSE, minimum_size=1024)


@app.get("/health")
def health() -> dict:
    configured = [p.id for p in MODEL_PRESETS if model_is_configured(p.id)]
    return {
        "status": "ok",
        "version": __version__,
        "coverage": "uk_europe",
        "models": configured,
    }


from .routes import boat as _boat_routes  # noqa: E402
from .routes import forecast as _forecast_routes  # noqa: E402
from .routes import geo as _geo_routes  # noqa: E402
from .routes import models as _models_routes  # noqa: E402
from .routes import prepare as _prepare_routes  # noqa: E402
from .routes import route as _route_routes  # noqa: E402

app.include_router(_models_routes.router)
app.include_router(_boat_routes.router)
app.include_router(_forecast_routes.router)
app.include_router(_geo_routes.router)
app.include_router(_prepare_routes.router)
app.include_router(_route_routes.router)
