"""/route: isochrone routing, one ModelRoute per model.

Local-first contract: /route uses cached forecasts (fetch ashore via
/forecasts/fetch). If a model isn't cached and the machine is online, we fetch it
live, cache it, and add a warning; if offline, that model is skipped with a
warning so the rest of the request still returns.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter

from .. import cache, store
from ..boat import Boat
from ..config import MODEL_PRESETS_BY_ID, TERRAIN_WIND_ENGINE
from ..currents import get_current_provider
from ..forecast import get_provider
from ..geo import context_for_course, course_bbox, load_default_land
from ..metocean import CurrentField, WindField
from ..router import IsochroneRouter, RouterParams
from ..schemas import ModelRoute, RouteRequest, RouteResponse
from ..terrain import get_terrain
from ..terrain_wind import get_terrain_wind

router = APIRouter(tags=["route"])


def _apply_terrain_wind(model_id: str, wind: WindField, req: RouteRequest) -> WindField:
    """Downscale the model wind with the configured terrain-wind engine (cached)."""
    engine = get_terrain_wind(TERRAIN_WIND_ENGINE)
    if engine is None:
        return wind
    bbox = course_bbox(req.course)
    key = cache.wind_key(f"{model_id}+terrain-{TERRAIN_WIND_ENGINE}", bbox, None)
    if cache.has_wind(key):
        return cache.load_wind(key)
    downscaled = engine.apply(wind, load_default_land(), get_terrain())
    cache.save_wind(key, downscaled)
    return downscaled


def _load_or_fetch_wind(model_id: str, req: RouteRequest, warnings: list[str]) -> WindField | None:
    preset = MODEL_PRESETS_BY_ID.get(model_id)
    if preset is None:
        warnings.append(f"{model_id}: unknown model preset, skipped")
        return None

    bbox = course_bbox(req.course)
    key = cache.wind_key(model_id, bbox, None)
    if cache.has_wind(key):
        return cache.load_wind(key)

    # Cache miss: try a live fetch (still local-first — result is cached to disk).
    try:
        horizon = 120
        field = get_provider(preset.connector).fetch(model_id, bbox, None, horizon)
        cache.save_wind(key, field)
        warnings.append(
            f"{model_id}: not cached, fetched live and cached. Pre-fetch via "
            f"/forecasts/fetch for fully offline routing."
        )
        return field
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{model_id}: not cached and live fetch failed ({exc}); skipped")
        return None


def _load_or_fetch_current(req: RouteRequest, warnings: list[str]) -> CurrentField | None:
    bbox = course_bbox(req.course)
    start = req.course.depart_utc
    end = start + timedelta(hours=120)
    # Prefer a cached Copernicus SMOC field, else cached FES, else fetch (FES offline).
    for source in ("copernicus-smoc", "fes"):
        key = cache.current_key(source, bbox, start.isoformat())
        if cache.has_current(key):
            return cache.load_current(key)
    for source in ("copernicus-smoc", "fes"):
        try:
            field = get_current_provider(source).fetch(bbox, start, end)
            cache.save_current(cache.current_key(source, bbox, start.isoformat()), field)
            if source != "copernicus-smoc":
                warnings.append("currents: Copernicus unavailable, using FES offline tides")
            return field
        except Exception:  # noqa: BLE001
            continue
    warnings.append("currents: no source available; routing without currents")
    return None


@router.post("/route", response_model=RouteResponse)
def compute_route(req: RouteRequest) -> RouteResponse:
    warnings: list[str] = []

    if not store.has_polar(req.boat.polar_id):
        warnings.append(f"Unknown polar_id '{req.boat.polar_id}'; using bundled sample.")
        polar = store.get_polar(store.ensure_sample())
    else:
        polar = store.get_polar(req.boat.polar_id)

    boat = Boat(req.boat, polar)
    geo = context_for_course(req.course)
    params = RouterParams(
        time_step_s=req.time_step_s,
        heading_step_deg=req.heading_step_deg,
    )

    current = _load_or_fetch_current(req, warnings) if req.use_currents else None

    routes: list[ModelRoute] = []
    for model_id in req.model_ids:
        wind = _load_or_fetch_wind(model_id, req, warnings)
        if wind is None:
            continue
        if req.use_terrain_wind:
            wind = _apply_terrain_wind(model_id, wind, req)
        engine = IsochroneRouter(
            boat=boat,
            wind=wind,
            geo=geo,
            current=current,
            params=params,
            use_currents=req.use_currents,
        )
        routes.append(engine.route(req.course, model_id))

    if not routes:
        warnings.append("No routes computed. Fetch forecasts for the selected models first.")

    return RouteResponse(routes=routes, warnings=warnings)
