"""/route: isochrone routing, one ModelRoute per model (wind-only).

Uses the same ensure_wind_window pipeline as the map scrubber. When the
isochrone hits forecast_exhausted, extends +3h on the same region and re-runs.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import numpy as np
from fastapi import APIRouter

from .. import store
from ..boat import Boat
from ..field_service import ensure_wind_window, forecast_horizon_for_course
from ..forecast.grib_source import ForecastRateLimitError
from ..geo import COURSE_WIND_MARGIN_DEG, context_for_course, course_bbox
from ..router import IsochroneRouter, RouterParams
from ..schemas import ModelRoute, RouteRequest, RouteResponse
from ..util.horizon import CHUNK_H

logger = logging.getLogger(__name__)
router = APIRouter(tags=["route"])

_EXTEND_H = CHUNK_H  # +3h, same as map prefetch
_MAX_EXTEND_ROUNDS = 20


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@router.post("/route", response_model=RouteResponse)
def compute_route(req: RouteRequest) -> RouteResponse:
    t0 = time.perf_counter()
    warnings: list[str] = []

    if not store.has_polar(req.boat.polar_id):
        warnings.append(f"Unknown polar_id '{req.boat.polar_id}'; using bundled sample.")
        polar = store.get_polar(store.ensure_sample())
    else:
        polar = store.get_polar(req.boat.polar_id)

    boat = Boat(req.boat, polar)
    try:
        t_geo = time.perf_counter()
        geo = context_for_course(req.course)
        logger.info("route geo %.0fms", (time.perf_counter() - t_geo) * 1000)
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
    depart = _to_utc(req.course.depart_utc)
    end = depart + timedelta(hours=horizon_h)
    bbox = course_bbox(req.course, margin_deg=COURSE_WIND_MARGIN_DEG)

    routes: list[ModelRoute] = []
    for model_id in req.model_ids:
        t_model = time.perf_counter()
        try:
            t_wind = time.perf_counter()
            wind = ensure_wind_window(model_id, bbox, start=depart, end=end)
            logger.info(
                "route %s wind initial %.0fms",
                model_id,
                (time.perf_counter() - t_wind) * 1000,
            )
        except ForecastRateLimitError as exc:
            warnings.append(str(exc))
            continue
        if wind is None:
            warnings.append(f"{model_id}: wind unavailable")
            continue

        route: ModelRoute | None = None
        for extend_round in range(_MAX_EXTEND_ROUNDS + 1):
            t_iso = time.perf_counter()
            engine = IsochroneRouter(
                boat=boat,
                wind=wind,
                geo=geo,
                params=params,
            )
            route = engine.route(req.course, model_id)
            logger.info(
                "route %s isochrone round=%d exhausted=%s %.0fms",
                model_id,
                extend_round,
                route.forecast_exhausted,
                (time.perf_counter() - t_iso) * 1000,
            )
            if not route.forecast_exhausted:
                break
            if extend_round >= _MAX_EXTEND_ROUNDS:
                last_h = int((wind.times[-1] - wind.times[0]) / 3600) if wind.n_times else 0
                warnings.append(
                    f"{model_id}: forecast exhausted after {last_h}h "
                    f"(extended {_MAX_EXTEND_ROUNDS}×{_EXTEND_H}h). Partial route returned."
                )
                break

            t_last = datetime.fromtimestamp(float(wind.times[-1]), UTC)
            t_ext_end = t_last + timedelta(hours=_EXTEND_H)
            logger.info(
                "route %s extend +%dh from %s",
                model_id,
                _EXTEND_H,
                t_last.isoformat(),
            )
            try:
                t_ext = time.perf_counter()
                ensure_wind_window(model_id, bbox, start=t_last, end=t_ext_end)
                wind = ensure_wind_window(model_id, bbox, start=depart, end=t_ext_end)
                logger.info(
                    "route %s extend wind %.0fms n_times=%s",
                    model_id,
                    (time.perf_counter() - t_ext) * 1000,
                    wind.n_times if wind is not None else 0,
                )
            except ForecastRateLimitError as exc:
                warnings.append(str(exc))
                break
            if wind is None:
                warnings.append(f"{model_id}: wind unavailable during extend")
                break

        if route is not None:
            routes.append(route)
        logger.info(
            "route %s total %.0fms",
            model_id,
            (time.perf_counter() - t_model) * 1000,
        )

    if not routes:
        warnings.append(
            "No routes computed. Wind data could not be fetched from GRIB sources. "
            "Check model availability for this area, or retry later."
        )

    logger.info(
        "route done models=%d routes=%d total=%.0fms",
        len(req.model_ids),
        len(routes),
        (time.perf_counter() - t0) * 1000,
    )
    return RouteResponse(routes=routes, warnings=warnings)
