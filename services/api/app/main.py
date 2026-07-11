"""FastAPI entrypoint.

API contract:
    POST /boat             -> validated BoatConfig + polar_id
    GET  /models           -> list[ModelPreset]
    GET  /models/available -> models covering a bbox
    GET  /wind/grid        -> LOD wind tile (single time)
    POST /course/prepare   -> prefetch wind + geo for a course
    POST /route            -> RouteResponse (one ModelRoute per model)
    GET  /elevation        -> {elevation_m, depth_m, on_land}
    GET  /health
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__

app = FastAPI(
    title="Sailing Weather Router",
    version=__version__,
    description="Local-first sailing weather router with LOD wind tiles.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "coverage": "global",
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
