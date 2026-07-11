"""/route: isochrone routing, one ModelRoute per model (wind-only)."""

from __future__ import annotations

import logging

import numpy as np
from fastapi import APIRouter

from .. import store
from ..boat import Boat
from ..geo import context_for_course, course_bbox
from ..metocean_service import forecast_horizon_for_course, load_or_fetch_wind_for_route
from ..router import IsochroneRouter, RouterParams
from ..schemas import ModelRoute, RouteRequest, RouteResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["route"])


@router.post("/route", response_model=RouteResponse)
def compute_route(req: RouteRequest) -> RouteResponse:
    warnings: list[str] = []

    if not store.has_polar(req.boat.polar_id):
        warnings.append(f"Unknown polar_id '{req.boat.polar_id}'; using bundled sample.")
        polar = store.get_polar(store.ensure_sample())
    else:
        polar = store.get_polar(req.boat.polar_id)

    boat = Boat(req.boat, polar)
    try:
        geo = context_for_course(req.course)
    except Exception as exc:  # noqa: BLE001
        logger.warning("geo context failed: %s", exc)
        warnings.append(f"land/depth data unavailable ({exc}); routing may be inaccurate")
        from ..geo import DepthField, GeoContext, LandMask

        geo = GeoContext.build(
            land=LandMask([]),
            depth=DepthField(np.array([0.0]), np.array([0.0]), np.array([[100.0]])),
            exclusion_rings=[],
            min_depth_m=req.course.min_depth_m,
        )
    params = RouterParams(
        time_step_s=req.time_step_s,
        heading_step_deg=req.heading_step_deg,
    )

    horizon_h = forecast_horizon_for_course(req.course)

    routes: list[ModelRoute] = []
    for model_id in req.model_ids:
        wind = load_or_fetch_wind_for_route(model_id, course_bbox(req.course), horizon_h, warnings)
        if wind is None:
            continue
        engine = IsochroneRouter(
            boat=boat,
            wind=wind,
            geo=geo,
            params=params,
        )
        route = engine.route(req.course, model_id)
        if route.forecast_exhausted:
            last_h = int((wind.times[-1] - wind.times[0]) / 3600)
            warnings.append(
                f"{model_id}: forecast exhausted after {last_h}h "
                f"(route needs ~{horizon_h}h). Partial route returned."
            )
        routes.append(route)

    if not routes:
        warnings.append(
            "No routes computed. Forecast data could not be fetched from Open-Meteo "
            "(free service, sometimes rate-limited). Try GFS/ECMWF/ICON only, or retry in a minute."
        )

    return RouteResponse(routes=routes, warnings=warnings)
