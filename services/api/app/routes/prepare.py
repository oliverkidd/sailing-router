"""Course-area prefetch: geo + wind for a padded bbox before routing."""

from __future__ import annotations

import logging
from datetime import UTC, timedelta

from fastapi import APIRouter

from ..config import GEO_CACHE_DIR
from ..field_service import get_area_field
from ..geo import course_bbox
from ..geo.fetch import ensure_geo_context
from ..schemas import (
    BBox,
    Course,
    CoursePrepareModelStatus,
    CoursePrepareRequest,
    CoursePrepareResponse,
)
from ..util.horizon import routing_horizon_h

logger = logging.getLogger(__name__)
router = APIRouter(tags=["prepare"])

_PREPARE_MARGIN_DEG = 0.75


def prepare_bbox(course: Course) -> BBox:
    return course_bbox(course, margin_deg=_PREPARE_MARGIN_DEG)


def _geo_cached(bbox: BBox) -> bool:
    key = bbox.key()
    return (GEO_CACHE_DIR / f"{key}_land.geojson").exists() and (
        GEO_CACHE_DIR / f"{key}_depth.npz"
    ).exists()


def _prepare_geo(course: Course, bbox: BBox) -> str:
    if _geo_cached(bbox):
        return "cached"
    try:
        ensure_geo_context(course, margin_deg=_PREPARE_MARGIN_DEG)
        return "fetched"
    except Exception as exc:  # noqa: BLE001
        logger.warning("prepare geo failed: %s", exc)
        return "error"


def _fetch_wind(model_id: str, bbox: BBox, course: Course, horizon_h: int) -> CoursePrepareModelStatus:
    depart = course.depart_utc
    if depart.tzinfo is None:
        depart = depart.replace(tzinfo=UTC)
    end = depart + timedelta(hours=horizon_h)
    field = get_area_field(model_id, bbox, start=depart, end=end)
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
    bbox = prepare_bbox(req.course)
    horizon_h = routing_horizon_h(req.course)
    geo_status = _prepare_geo(req.course, bbox)

    models: list[CoursePrepareModelStatus] = [
        _fetch_wind(model_id, bbox, req.course, horizon_h) for model_id in req.model_ids
    ]

    return CoursePrepareResponse(
        bbox=bbox,
        horizon_h=horizon_h,
        geo=geo_status,  # type: ignore[arg-type]
        models=models,
    )
