"""Central wind field service: canonical normalize + incremental cache."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from . import catalog, store_wind
from .config import MODEL_PRESETS_BY_ID, WIND_TTL_S, model_is_configured
from .forecast import get_source
from .forecast.grib_source import ForecastRateLimitError
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

# Tiny in-process hint only — canonical fields live on disk as Zarr under
# data/cache/forecast/. Holding many full grids here blows RAM (Cursor /
# API OOM / workspace disconnects). Re-read from Zarr on miss.
_LRU_MAX = 1
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
    Raises ForecastRateLimitError when the upstream CDN rate-limits.
    """
    t_all = time.perf_counter()
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

        # Covering-region cache hits can have a denser/wider grid than this
        # request's canonical axes — regrid onto (lats_t, lons_t) before use.
        if cached is not None and (
            cached.lats.size != lats_t.size
            or cached.lons.size != lons_t.size
            or not np.allclose(cached.lats, lats_t)
            or not np.allclose(cached.lons, lons_t)
        ):
            try:
                t_rg = time.perf_counter()
                cached = cached.regrid(lats_t, lons_t)
                logger.info(
                    "wind %s cache-regrid %dx%d %.0fms",
                    model_id,
                    lats_t.size,
                    lons_t.size,
                    (time.perf_counter() - t_rg) * 1000,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cache regrid failed for %s (discarding covering cache): %s",
                    model_id,
                    exc,
                )
                cached = None

        have: set[float] = set()
        if cached is not None:
            # Round to whole seconds so float noise doesn't create false misses.
            have = {float(round(t)) for t in cached.times.tolist()}
        missing = [h for h in needed if float(round(h)) not in have]
        if not missing and cached is not None:
            logger.info(
                "wind %s cache-hit region=%s hours=%d grid=%dx%d total=%.0fms",
                model_id,
                region.key(),
                len(needed),
                lats_t.size,
                lons_t.size,
                (time.perf_counter() - t_all) * 1000,
            )
            return cached

        # Fetch only the native steps bracketing the missing hours.
        m_start = datetime.fromtimestamp(min(missing), UTC)
        m_end = datetime.fromtimestamp(max(missing), UTC)
        steps = src.steps_for_window(cyc, m_start, m_end)
        logger.info(
            "wind %s cache-miss region=%s missing=%d/%d steps=%d grid=%dx%d",
            model_id,
            region.key(),
            len(missing),
            len(needed),
            len(steps),
            lats_t.size,
            lons_t.size,
        )
        try:
            t_fetch = time.perf_counter()
            native = src.fetch(region, cyc, steps)
            fetch_ms = (time.perf_counter() - t_fetch) * 1000

            t_norm = time.perf_counter()
            canon = native.regrid(lats_t, lons_t).resample_hourly(
                min(missing), max(missing)
            )
            merged = WindField.concat_time([cached, canon]) if cached is not None else canon
            norm_ms = (time.perf_counter() - t_norm) * 1000

            t_write = time.perf_counter()
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
            write_ms = (time.perf_counter() - t_write) * 1000
            logger.info(
                "wind %s fetched fetch=%.0fms regrid+resample=%.0fms zarr=%.0fms "
                "total=%.0fms n_times=%d",
                model_id,
                fetch_ms,
                norm_ms,
                write_ms,
                (time.perf_counter() - t_all) * 1000,
                merged.n_times,
            )
            return merged
        except ForecastRateLimitError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("GRIB fetch %s failed: %s", model_id, exc)
            # If we had a partial cache that covers something, return it.
            return cached


def ensure_wind_window(
    model_id: str,
    bbox: BBox,
    *,
    start: datetime,
    end: datetime,
) -> WindField | None:
    """Ensure hourly canonical wind for [start,end] on region_bbox(bbox).

    Same path as map /wind/area (minus display downsample). Used by prepare + /route.
    Raises ForecastRateLimitError; returns None if unavailable.
    """
    return get_area_field(model_id, bbox, start=start, end=end)


def get_display_series(
    model_id: str,
    bbox: BBox,
    *,
    start: datetime,
    end: datetime,
) -> dict | None:
    """Return a multi-hour series payload; downsample under MAX_DISPLAY_PTS when large."""
    field = get_area_field(model_id, bbox, start=start, end=end)
    if field is None:
        return None
    nlat, nlon = budget_dims(bbox, CANON_RES_DEG)
    return display_series_payload(field, bbox, nlat, nlon, start, end)


def forecast_horizon_for_course(course) -> int:
    return routing_horizon_h(course)
