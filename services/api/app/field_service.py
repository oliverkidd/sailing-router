"""Central wind field service: canonical normalize + incremental cache."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from . import catalog, store_wind
from .config import MODEL_PRESETS_BY_ID, WIND_TTL_S, model_is_configured
from .forecast import get_source
from .metocean import WindField
from .overlays.grid import display_series_payload
from .schemas import BBox
from .util.bbox import (
    CANON_RES_DEG,
    budget_dims,
    canonical_grid,
    hourly_epochs,
    region_bbox,
)
from .util.horizon import routing_horizon_h
from .util.timeparse import parse_utc_iso

logger = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# In-memory LRU of recently read fields (keyed by zarr path).
_LRU_MAX = 8
_lru: OrderedDict[str, WindField] = OrderedDict()
_lru_guard = threading.Lock()


def _lru_get(path: str) -> WindField | None:
    with _lru_guard:
        field = _lru.get(path)
        if field is not None:
            _lru.move_to_end(path)
        return field


def _lru_put(path: str, field: WindField) -> None:
    with _lru_guard:
        _lru[path] = field
        _lru.move_to_end(path)
        while len(_lru) > _LRU_MAX:
            _lru.popitem(last=False)


def _read_cached(path: str) -> WindField:
    hit = _lru_get(path)
    if hit is not None:
        return hit
    field = store_wind.read_field(Path(path))
    _lru_put(path, field)
    return field


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_area_field(
    model_id: str,
    bbox: BBox,
    *,
    start: datetime,
    end: datetime,
    cycle: str = "latest",
) -> WindField | None:
    """Load or fetch a canonical (500 m + hourly) WindField covering bbox/window.

    Returns None on failure (caller reports 'unavailable'). No Open-Meteo fallback.
    Delta-extends the cached field when only some hours are missing.
    """
    if MODEL_PRESETS_BY_ID.get(model_id) is None:
        logger.warning("unknown model_id %s", model_id)
        return None
    if not model_is_configured(model_id):
        logger.warning("model %s not configured (missing API key)", model_id)
        return None

    try:
        src = get_source(model_id)
    except KeyError:
        logger.warning("no GRIB source for %s", model_id)
        return None

    now = datetime.now(UTC)
    try:
        cyc = src.latest_cycle(now) if cycle == "latest" else parse_utc_iso(cycle)
        cyc = _to_utc(cyc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cycle resolve failed for %s: %s", model_id, exc)
        return None

    start = _to_utc(start)
    end = _to_utc(end)
    if end < start:
        start, end = end, start

    region = region_bbox(bbox)
    try:
        lats_t, lons_t = canonical_grid(region)
    except ValueError as exc:
        logger.warning("canonical grid failed for %s: %s", model_id, exc)
        return None

    key = f"{model_id}|{region.key()}|{cyc.isoformat()}"
    needed = hourly_epochs(start, end)
    if not needed:
        return None

    lock = _lock_for(key)
    with lock:
        row = catalog.find_region_field(model_id, cyc.isoformat(), region)
        cached: WindField | None = None
        if row is not None and Path(row.zarr_path).exists():
            try:
                cached = _read_cached(row.zarr_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cache read failed %s: %s", row.zarr_path, exc)
                cached = None

        have: set[float] = set()
        if cached is not None:
            # Round to whole seconds so float noise doesn't create false misses.
            have = {float(round(t)) for t in cached.times.tolist()}
        missing = [h for h in needed if float(round(h)) not in have]
        if not missing and cached is not None:
            return cached

        # Fetch only the native steps bracketing the missing hours.
        m_start = datetime.fromtimestamp(min(missing), UTC)
        m_end = datetime.fromtimestamp(max(missing), UTC)
        steps = src.steps_for_window(cyc, m_start, m_end)
        try:
            native = src.fetch(region, cyc, steps)
            canon = native.regrid(lats_t, lons_t).resample_hourly(
                min(missing), max(missing)
            )
            merged = WindField.concat_time([cached, canon]) if cached is not None else canon
            # Rewrite whole Zarr (simplest correct option; append is a later opt).
            path = store_wind.write_field(merged, cyc, region)
            catalog.upsert(
                model_id,
                cyc,
                region,
                merged,
                path,
                WIND_TTL_S,
                res_deg=CANON_RES_DEG,
            )
            _lru_put(str(path), merged)
            return merged
        except Exception as exc:  # noqa: BLE001
            logger.warning("GRIB fetch %s failed: %s", model_id, exc)
            # If we had a partial cache that covers something, return it.
            return cached


def get_display_series(
    model_id: str,
    bbox: BBox,
    *,
    start: datetime,
    end: datetime,
) -> dict | None:
    """Return a multi-hour series payload; downsample only when viewport is large."""
    field = get_area_field(model_id, bbox, start=start, end=end)
    if field is None:
        return None
    nlat, nlon = budget_dims(bbox, CANON_RES_DEG)
    return display_series_payload(field, bbox, nlat, nlon, start, end)


def forecast_horizon_for_course(course) -> int:
    return routing_horizon_h(course)
