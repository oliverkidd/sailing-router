"""/route: isochrone routing, one ModelRoute per model (wind-only)."""

from __future__ import annotations

import logging
from datetime import UTC, timedelta

import numpy as np
from fastapi import APIRouter

from .. import store
from ..boat import Boat
from ..field_service import forecast_horizon_for_course, get_area_field
from ..geo import context_for_course, course_bbox
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
    depart = req.course.depart_utc
    if depart.tzinfo is None:
        depart = depart.replace(tzinfo=UTC)
    end = depart + timedelta(hours=horizon_h)

    routes: list[ModelRoute] = []
    for model_id in req.model_ids:
        wind = get_area_field(
            model_id, course_bbox(req.course), start=depart, end=end
        )
        if wind is None:
            warnings.append(f"{model_id}: wind unavailable")
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
            "No routes computed. Wind data could not be fetched from GRIB sources. "
            "Check model availability for this area, or retry later."
        )

    return RouteResponse(routes=routes, warnings=warnings)
