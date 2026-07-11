"""Course-area prefetch: geo + wind for a padded bbox before routing."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from ..config import GEO_CACHE_DIR, MODEL_PRESETS_BY_ID
from ..geo import course_bbox
from ..geo.fetch import ensure_geo_context
from ..metocean_service import (
    _DISPLAY_FALLBACKS,
    _MODEL_FALLBACKS,
    _cache_covers_horizon,
    fetch_wind,
    load_wind,
)
from ..schemas import (
    BBox,
    Course,
    CoursePrepareModelStatus,
    CoursePrepareRequest,
    CoursePrepareResponse,
)
from ..util.bbox import align_tile_bbox, route_grid_dims
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


def _fetch_wind(model_id: str, bbox: BBox, horizon_h: int) -> CoursePrepareModelStatus:
    preset = MODEL_PRESETS_BY_ID.get(model_id)
    if preset is None:
        return CoursePrepareModelStatus(model_id=model_id, status="error", message="unknown model")

    snapped = align_tile_bbox(bbox)
    dims = route_grid_dims(snapped, preset.resolution_deg)
    chain = (model_id, *_MODEL_FALLBACKS.get(model_id, _DISPLAY_FALLBACKS))
    last_error: str | None = None

    for mid in chain:
        mid_preset = MODEL_PRESETS_BY_ID.get(mid)
        mid_dims = (
            route_grid_dims(snapped, mid_preset.resolution_deg) if mid_preset else dims
        )
        existing = load_wind(mid, snapped, dims=mid_dims)
        if existing is not None and _cache_covers_horizon(existing, horizon_h):
            msg = f"using {mid} as substitute" if mid != model_id else None
            return CoursePrepareModelStatus(
                model_id=model_id, status="cached", n_times=existing.n_times, message=msg
            )

        try:
            field = fetch_wind(mid, snapped, horizon_h=horizon_h, grid_shape=mid_dims)
            msg = f"fetched via {mid} fallback" if mid != model_id else None
            return CoursePrepareModelStatus(
                model_id=model_id, status="fetched", n_times=field.n_times, message=msg
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("prepare wind %s failed: %s", mid, exc)
            continue

    return CoursePrepareModelStatus(
        model_id=model_id,
        status="error",
        message=last_error or "Open-Meteo forecast request failed",
    )


@router.post("/course/prepare", response_model=CoursePrepareResponse)
def prepare_course(req: CoursePrepareRequest) -> CoursePrepareResponse:
    bbox = prepare_bbox(req.course)
    horizon_h = routing_horizon_h(req.course)
    geo_status = _prepare_geo(req.course, bbox)

    models: list[CoursePrepareModelStatus] = [
        _fetch_wind(model_id, bbox, horizon_h) for model_id in req.model_ids
    ]

    return CoursePrepareResponse(
        bbox=bbox,
        horizon_h=horizon_h,
        geo=geo_status,  # type: ignore[arg-type]
        models=models,
    )
