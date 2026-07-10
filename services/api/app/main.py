"""FastAPI entrypoint. Endpoints are wired milestone-by-milestone.

API contract (spec §8):
    POST /boat             -> validated BoatConfig + polar_id
    GET  /models           -> list[ModelPreset]
    POST /forecasts/fetch  -> per-model status + cache keys
    POST /currents/fetch   -> status + cache key
    POST /route            -> RouteResponse (one ModelRoute per model)
    GET  /elevation        -> {elevation_m}
    GET  /health
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import MODEL_PRESETS, REGION_BBOX
from .schemas import ModelPreset

app = FastAPI(
    title="Sailing Weather Router",
    version=__version__,
    description="Local-first UK + Mediterranean static route planner. No US systems.",
)

# Local-first dev: allow the Next.js dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "region": REGION_BBOX}


@app.get("/models", response_model=list[ModelPreset])
def get_models() -> list[ModelPreset]:
    """The European model presets (+ colors) offered in the UI."""
    return MODEL_PRESETS


# Endpoints added in later milestones:
from .routes import boat as _boat_routes  # noqa: E402
from .routes import forecast as _forecast_routes  # noqa: E402
from .routes import geo as _geo_routes  # noqa: E402
from .routes import route as _route_routes  # noqa: E402

app.include_router(_boat_routes.router)
app.include_router(_forecast_routes.router)
app.include_router(_geo_routes.router)
app.include_router(_route_routes.router)
