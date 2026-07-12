"""Course-area prefetch: geo + wind for a padded bbox before routing.

Uses the same ensure_wind_window / region_bbox / hourly Zarr path as the map scrubber.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, timedelta

from fastapi import APIRouter

from ..config import GEO_CACHE_DIR
from ..field_service import ensure_wind_window
from ..forecast.grib_source import ForecastRateLimitError
from ..geo import COURSE_WIND_MARGIN_DEG, course_bbox
from ..geo.fetch import ensure_geo_context
from ..schemas import (
    BBox,
    Course,
    CoursePrepareModelStatus,
    CoursePrepareRequest,
    CoursePrepareResponse,
)
from ..util.horizon import iter_hour_chunks, routing_horizon_h

logger = logging.getLogger(__name__)
router = APIRouter(tags=["prepare"])

_MODEL_WORKERS = 3


def prepare_bbox(course: Course) -> BBox:
    """Same corridor as /route so Zarr region keys match."""
    return course_bbox(course, margin_deg=COURSE_WIND_MARGIN_DEG)


def _geo_cached(bbox: BBox) -> bool:
    key = bbox.key()
    return (GEO_CACHE_DIR / f"{key}_land.geojson").exists() and (
        GEO_CACHE_DIR / f"{key}_depth.npz"
    ).exists()


def _prepare_geo(course: Course, bbox: BBox) -> str:
    if _geo_cached(bbox):
        return "cached"
    try:
        ensure_geo_context(course, margin_deg=COURSE_WIND_MARGIN_DEG)
        return "fetched"
    except Exception as exc:  # noqa: BLE001
        logger.warning("prepare geo failed: %s", exc)
        return "error"


def _prefetch_model(
    model_id: str, bbox: BBox, course: Course, horizon_h: int
) -> CoursePrepareModelStatus:
    """Fill [depart, depart+horizon] in +3h chunks via the shared wind pipeline."""
    t_model = time.perf_counter()
    depart = course.depart_utc
    if depart.tzinfo is None:
        depart = depart.replace(tzinfo=UTC)
    end = depart + timedelta(hours=horizon_h)
    field = None
    try:
        for chunk_start, chunk_end in iter_hour_chunks(depart, end):
            t_chunk = time.perf_counter()
            field = ensure_wind_window(
                model_id, bbox, start=chunk_start, end=chunk_end
            )
            logger.info(
                "prepare %s chunk %s→%s %.0fms",
                model_id,
                chunk_start.isoformat(),
                chunk_end.isoformat(),
                (time.perf_counter() - t_chunk) * 1000,
            )
            if field is None:
                return CoursePrepareModelStatus(
                    model_id=model_id,
                    status="error",
                    message="wind unavailable",
                )
    except ForecastRateLimitError as exc:
        logger.warning("prepare wind rate-limited %s: %s", model_id, exc)
        return CoursePrepareModelStatus(
            model_id=model_id,
            status="error",
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("prepare wind failed %s: %s", model_id, exc)
        return CoursePrepareModelStatus(
            model_id=model_id,
            status="error",
            message=str(exc) or "wind unavailable",
        )

    logger.info(
        "prepare %s done horizon=%dh n_times=%s total=%.0fms",
        model_id,
        horizon_h,
        field.n_times if field is not None else 0,
        (time.perf_counter() - t_model) * 1000,
    )
    if field is None:
        return CoursePrepareModelStatus(
            model_id=model_id,
            status="error",
            message="wind unavailable",
        )
    return CoursePrepareModelStatus(
        model_id=model_id,
        status="fetched",
        n_times=field.n_times,
    )


@router.post("/course/prepare", response_model=CoursePrepareResponse)
def prepare_course(req: CoursePrepareRequest) -> CoursePrepareResponse:
    t0 = time.perf_counter()
    bbox = prepare_bbox(req.course)
    horizon_h = routing_horizon_h(req.course)
    geo_status = _prepare_geo(req.course, bbox)

    models: list[CoursePrepareModelStatus] = []
    if not req.model_ids:
        return CoursePrepareResponse(
            bbox=bbox,
            horizon_h=horizon_h,
            geo=geo_status,  # type: ignore[arg-type]
            models=models,
        )

    workers = min(_MODEL_WORKERS, len(req.model_ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_prefetch_model, mid, bbox, req.course, horizon_h): mid
            for mid in req.model_ids
        }
        by_id: dict[str, CoursePrepareModelStatus] = {}
        for fut in as_completed(futures):
            status = fut.result()
            by_id[status.model_id] = status
        # Preserve request order for the UI.
        models = [by_id[mid] for mid in req.model_ids if mid in by_id]

    logger.info(
        "prepare total=%.0fms horizon=%dh models=%d geo=%s",
        (time.perf_counter() - t0) * 1000,
        horizon_h,
        len(models),
        geo_status,
    )
    return CoursePrepareResponse(
        bbox=bbox,
        horizon_h=horizon_h,
        geo=geo_status,  # type: ignore[arg-type]
        models=models,
    )
