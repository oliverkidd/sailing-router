"""Shared load-or-fetch helpers for wind fields."""

from __future__ import annotations

import logging

from . import cache
from .config import MODEL_PRESETS_BY_ID
from .forecast import get_provider
from .metocean import WindField
from .schemas import BBox
from .util.bbox import align_tile_bbox, route_grid_dims
from .util.horizon import routing_horizon_h

logger = logging.getLogger(__name__)

_OVERLAY_HORIZON_H = 72
_ROUTE_HORIZON_H = 168  # 7 days


def _wind_span_h(field: WindField) -> float:
    return (field.times[-1] - field.times[0]) / 3600.0


def _cache_covers_horizon(field: WindField, horizon_h: float) -> bool:
    return _wind_span_h(field) >= horizon_h * 0.85


# When a regional/high-res model fails, try these substitutes (same connector).
_MODEL_FALLBACKS: dict[str, tuple[str, ...]] = {
    "icon_eu": ("icon", "gfs", "ecmwf"),
    "icon_d2": ("icon_eu", "icon", "gfs"),
    "arome": ("arpege_eu", "arpege", "gfs"),
    "arpege_eu": ("arpege", "gfs", "ecmwf"),
    "harmonie": ("icon_eu", "icon", "gfs"),
    "gem": ("gfs", "ecmwf"),
    "ukmo": ("gfs", "ecmwf"),
    "arpege": ("gfs", "ecmwf"),
    "icon": ("gfs", "ecmwf"),
    "gfs": ("ecmwf", "icon"),
    "ecmwf": ("gfs", "icon"),
}

_DISPLAY_FALLBACKS = ("gfs", "ecmwf", "icon")


def load_wind(
    model_id: str,
    bbox: BBox,
    *,
    fresh: bool = False,
    dims: tuple[int, int] | None = None,
) -> WindField | None:
    key = cache.wind_key(model_id, bbox, None, dims=dims)
    if cache.has_wind(key) and (not fresh or cache.wind_is_fresh(key)):
        return cache.load_wind(key)
    # Also try legacy keys without dims for routing reuse.
    if dims is not None:
        legacy = cache.wind_key(model_id, bbox, None)
        if cache.has_wind(legacy) and (not fresh or cache.wind_is_fresh(legacy)):
            return cache.load_wind(legacy)
    covering = cache.find_covering_wind(model_id, bbox)
    if covering is not None:
        return covering
    return cache.find_overlapping_wind(model_id, bbox)


def fetch_wind(
    model_id: str,
    bbox: BBox,
    *,
    horizon_h: int | None = None,
    grid_shape: tuple[int, int] | None = None,
) -> WindField:
    preset = MODEL_PRESETS_BY_ID.get(model_id)
    if preset is None:
        raise ValueError(f"Unknown model preset '{model_id}'")
    snapped = align_tile_bbox(bbox)
    h = horizon_h or _OVERLAY_HORIZON_H
    provider = get_provider(preset.connector)
    kwargs: dict = {}
    if grid_shape is not None:
        kwargs["grid_shape"] = grid_shape
    field = provider.fetch(model_id, snapped, None, h, **kwargs)
    cache.save_wind(cache.wind_key(model_id, snapped, None, dims=grid_shape), field)
    return field


def load_or_fetch_wind_tile(
    model_id: str,
    bbox: BBox,
    *,
    nlat: int,
    nlon: int,
    horizon_h: int | None = None,
) -> tuple[WindField | None, str | None]:
    """Load or fetch a single LOD tile at explicit dims. Returns (field, error)."""
    snapped = align_tile_bbox(bbox)
    dims = (nlat, nlon)
    chain = (model_id, *_MODEL_FALLBACKS.get(model_id, _DISPLAY_FALLBACKS))
    last_error: str | None = None
    h = horizon_h or _OVERLAY_HORIZON_H

    for mid in chain:
        existing = load_wind(mid, snapped, dims=dims)
        if existing is not None:
            return existing, None
        try:
            return fetch_wind(mid, snapped, horizon_h=h, grid_shape=dims), None
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("tile wind fetch %s failed: %s", mid, exc)

    return None, last_error or "Open-Meteo forecast request failed (service may be overloaded)"


def load_or_fetch_wind_for_route(
    model_id: str,
    bbox: BBox,
    course_horizon_h: int,
    warnings: list[str],
) -> WindField | None:
    if MODEL_PRESETS_BY_ID.get(model_id) is None:
        warnings.append(f"{model_id}: unknown model preset, skipped")
        return None

    horizon = max(course_horizon_h, _ROUTE_HORIZON_H)
    snapped = align_tile_bbox(bbox)
    preset = MODEL_PRESETS_BY_ID[model_id]
    dims = route_grid_dims(snapped, preset.resolution_deg)

    last_error: str | None = None
    for mid in (model_id, *_MODEL_FALLBACKS.get(model_id, _DISPLAY_FALLBACKS)):
        existing = load_wind(mid, snapped, dims=dims)
        if existing is not None and _cache_covers_horizon(existing, horizon):
            if mid != model_id:
                warnings.append(f"{model_id}: using cached {mid} data as substitute")
            return existing

        covering = cache.find_covering_wind(mid, snapped)
        if covering is not None and _cache_covers_horizon(covering, horizon):
            if mid != model_id:
                warnings.append(f"{model_id}: using cached {mid} data as substitute")
            return covering

        try:
            mid_preset = MODEL_PRESETS_BY_ID.get(mid)
            mid_dims = (
                route_grid_dims(snapped, mid_preset.resolution_deg)
                if mid_preset
                else dims
            )
            field = fetch_wind(mid, snapped, horizon_h=horizon, grid_shape=mid_dims)
            if mid != model_id:
                warnings.append(f"{model_id}: live fetch failed; routed with {mid} data")
            return field
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("route wind fetch %s failed: %s", mid, exc)
            continue

    detail = f" ({last_error})" if last_error else ""
    warnings.append(
        f"{model_id}: forecast fetch failed{detail}. "
        "Open-Meteo is free but rate-limited — try GFS/ECMWF/ICON or retry in a minute."
    )
    return None


def forecast_horizon_for_course(course) -> int:
    return routing_horizon_h(course)
