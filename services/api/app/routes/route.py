"""/route: isochrone routing, one ModelRoute per model (wind-only).

Uses the same ensure_wind_window pipeline as the map scrubber. When the
isochrone hits forecast_exhausted, extends +3h on the same region and re-runs.
Also exposes POST /route/stream (SSE) for live stage/leg progress.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import store
from ..boat import Boat
from ..field_service import ensure_wind_window
from ..forecast.grib_source import ForecastRateLimitError
from ..geo import COURSE_WIND_MARGIN_DEG, GeoContext, course_bbox
from ..geo.fetch import fetch_depth_for_bbox, fetch_land_for_bbox
from ..router import IsochroneRouter, ProgressCallback, RouterParams
from ..schemas import ModelRoute, RouteRequest, RouteResponse
from ..util.horizon import CHUNK_H, initial_route_horizon_h

logger = logging.getLogger(__name__)
router = APIRouter(tags=["route"])

_EXTEND_H = CHUNK_H  # +3h, same as map prefetch
_MAX_EXTEND_ROUNDS = 20
_SENTINEL = object()
_MODEL_WORKERS = 3


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _run_route(
    req: RouteRequest,
    on_progress: ProgressCallback | None = None,
) -> RouteResponse:
    """Shared routing core used by /route and /route/stream."""
    t0 = time.perf_counter()
    warnings: list[str] = []
    emit: ProgressCallback = on_progress or (lambda _e: None)

    if not store.has_polar(req.boat.polar_id):
        warnings.append(f"Unknown polar_id '{req.boat.polar_id}'; using bundled sample.")
        polar = store.get_polar(store.ensure_sample())
    else:
        polar = store.get_polar(req.boat.polar_id)

    boat = Boat(req.boat, polar)
    try:
        bbox_geo = course_bbox(req.course, margin_deg=COURSE_WIND_MARGIN_DEG)
        emit({"type": "stage", "key": "land", "status": "start"})
        t_land = time.perf_counter()
        land = fetch_land_for_bbox(bbox_geo)
        land_ms = (time.perf_counter() - t_land) * 1000
        logger.info("route land %.0fms", land_ms)
        emit({"type": "stage", "key": "land", "status": "done", "ms": round(land_ms, 1)})

        emit({"type": "stage", "key": "depth", "status": "start"})
        t_depth = time.perf_counter()
        depth = fetch_depth_for_bbox(bbox_geo, land)
        depth_ms = (time.perf_counter() - t_depth) * 1000
        logger.info("route depth %.0fms", depth_ms)
        emit({"type": "stage", "key": "depth", "status": "done", "ms": round(depth_ms, 1)})

        geo = GeoContext.build(
            land=land,
            depth=depth,
            exclusion_rings=[[tuple(p) for p in ring] for ring in req.course.exclusions],
            min_depth_m=req.course.min_depth_m,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("geo context failed: %s", exc)
        msg = (
            f"land/depth chart data unavailable ({exc}); "
            "refusing to route over fabricated water. "
            "Ensure region_depth.tif and OSM land are built "
            "(build_depth_tiles.py / fetch_osm_land.py)."
        )
        warnings.append(msg)
        emit({"type": "error", "message": msg})
        return RouteResponse(routes=[], warnings=warnings)

    params = RouterParams(
        time_step_s=req.time_step_s,
        heading_step_deg=req.heading_step_deg,
    )

    # Short upfront window; +3h extend covers longer courses without a 45s cold fetch.
    horizon_h = initial_route_horizon_h(req.course)
    depart = _to_utc(req.course.depart_utc)
    end = depart + timedelta(hours=horizon_h)
    bbox = course_bbox(req.course, margin_deg=COURSE_WIND_MARGIN_DEG)

    def _route_one(model_id: str) -> tuple[ModelRoute | None, list[str]]:
        local_warnings: list[str] = []
        t_model = time.perf_counter()
        try:
            emit(
                {
                    "type": "stage",
                    "key": "wind",
                    "model_id": model_id,
                    "status": "start",
                }
            )
            t_wind = time.perf_counter()
            wind = ensure_wind_window(model_id, bbox, start=depart, end=end)
            wind_ms = (time.perf_counter() - t_wind) * 1000
            logger.info("route %s wind initial %.0fms horizon=%dh", model_id, wind_ms, horizon_h)
            emit(
                {
                    "type": "stage",
                    "key": "wind",
                    "model_id": model_id,
                    "status": "done",
                    "ms": round(wind_ms, 1),
                    "n_times": wind.n_times if wind is not None else 0,
                }
            )
        except ForecastRateLimitError as exc:
            local_warnings.append(str(exc))
            emit({"type": "warning", "message": str(exc)})
            return None, local_warnings
        if wind is None:
            msg = f"{model_id}: wind unavailable"
            local_warnings.append(msg)
            emit({"type": "warning", "message": msg})
            return None, local_warnings

        route: ModelRoute | None = None
        for extend_round in range(_MAX_EXTEND_ROUNDS + 1):
            t_iso = time.perf_counter()
            engine = IsochroneRouter(
                boat=boat,
                wind=wind,
                geo=geo,
                params=params,
                on_progress=on_progress,
                model_id=model_id,
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
                msg = (
                    f"{model_id}: forecast exhausted after {last_h}h "
                    f"(extended {_MAX_EXTEND_ROUNDS}×{_EXTEND_H}h). Partial route returned."
                )
                local_warnings.append(msg)
                emit({"type": "warning", "message": msg})
                break

            t_last = datetime.fromtimestamp(float(wind.times[-1]), UTC)
            t_ext_end = t_last + timedelta(hours=_EXTEND_H)
            logger.info(
                "route %s extend +%dh from %s",
                model_id,
                _EXTEND_H,
                t_last.isoformat(),
            )
            emit(
                {
                    "type": "stage",
                    "key": "wind_extend",
                    "model_id": model_id,
                    "status": "start",
                    "extend_h": _EXTEND_H,
                }
            )
            try:
                t_ext = time.perf_counter()
                ensure_wind_window(model_id, bbox, start=t_last, end=t_ext_end)
                wind = ensure_wind_window(model_id, bbox, start=depart, end=t_ext_end)
                ext_ms = (time.perf_counter() - t_ext) * 1000
                logger.info(
                    "route %s extend wind %.0fms n_times=%s",
                    model_id,
                    ext_ms,
                    wind.n_times if wind is not None else 0,
                )
                emit(
                    {
                        "type": "stage",
                        "key": "wind_extend",
                        "model_id": model_id,
                        "status": "done",
                        "ms": round(ext_ms, 1),
                        "n_times": wind.n_times if wind is not None else 0,
                    }
                )
            except ForecastRateLimitError as exc:
                local_warnings.append(str(exc))
                emit({"type": "warning", "message": str(exc)})
                break
            if wind is None:
                msg = f"{model_id}: wind unavailable during extend"
                local_warnings.append(msg)
                emit({"type": "warning", "message": msg})
                break

        if route is not None:
            for w in route.warnings:
                if w not in local_warnings:
                    local_warnings.append(w)
                    emit({"type": "warning", "message": w})
            if route.unreachable:
                msg = f"{model_id}: route unfinished (mark unreachable)"
                if msg not in local_warnings:
                    local_warnings.append(msg)
                    emit({"type": "warning", "message": msg})
        logger.info(
            "route %s total %.0fms",
            model_id,
            (time.perf_counter() - t_model) * 1000,
        )
        return route, local_warnings

    routes: list[ModelRoute] = []
    workers = max(1, min(_MODEL_WORKERS, len(req.model_ids)))
    # Preserve request order when collecting parallel results.
    results_by_id: dict[str, tuple[ModelRoute | None, list[str]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_route_one, mid): mid for mid in req.model_ids}
        for fut in as_completed(futs):
            mid = futs[fut]
            try:
                results_by_id[mid] = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.exception("route %s failed", mid)
                results_by_id[mid] = (None, [f"{mid}: {exc}"])

    for mid in req.model_ids:
        route, local_warnings = results_by_id.get(mid, (None, []))
        for w in local_warnings:
            if w not in warnings:
                warnings.append(w)
        if route is not None:
            routes.append(route)

    if not routes:
        msg = (
            "No routes computed. Wind data could not be fetched from GRIB sources. "
            "Check model availability for this area, or retry later."
        )
        warnings.append(msg)
        emit({"type": "error", "message": msg})

    total_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "route done models=%d routes=%d total=%.0fms",
        len(req.model_ids),
        len(routes),
        total_ms,
    )
    return RouteResponse(routes=routes, warnings=warnings)


@router.post("/route", response_model=RouteResponse)
def compute_route(req: RouteRequest) -> RouteResponse:
    return _run_route(req)


def _serialize_result(res: RouteResponse) -> dict[str, Any]:
    """JSON-safe RouteResponse for the SSE result event."""
    return json.loads(res.model_dump_json())


@router.post("/route/stream")
def compute_route_stream(req: RouteRequest) -> StreamingResponse:
    """SSE stream of stage/leg progress, then a final result event."""
    q: queue.Queue[Any] = queue.Queue()

    def on_progress(event: dict[str, Any]) -> None:
        q.put(event)

    def worker() -> None:
        try:
            result = _run_route(req, on_progress=on_progress)
            q.put({"type": "result", "routes": _serialize_result(result)["routes"],
                   "warnings": result.warnings})
        except Exception as exc:  # noqa: BLE001
            logger.exception("route stream failed")
            q.put({"type": "error", "message": str(exc)})
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=worker, name="route-stream", daemon=True)
    thread.start()

    def event_gen() -> Iterator[str]:
        while True:
            item = q.get()
            if item is _SENTINEL:
                break
            yield _sse(item)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
