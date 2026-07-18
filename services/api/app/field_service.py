"""Central wind field service: native-res forcing cache + WindProvider seam."""

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
from .schemas import BBox
from .util.bbox import (
    canonical_grid,
    hourly_epochs,
    region_bbox,
)
from .util.horizon import routing_horizon_h
from .util.timeparse import parse_utc_iso
from .wind.provider import get_provider

logger = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# Native-res grids are smaller than the old 500 m grids — keep a few warm.
_LRU_MAX = 4
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


def _forcing_res_deg(model_id: str) -> float:
    """Store forcing at the model's native resolution (not 500 m)."""
    preset = MODEL_PRESETS_BY_ID.get(model_id)
    if preset is None or preset.resolution_deg <= 0:
        return 0.25
    return float(preset.resolution_deg)


def get_area_field(
    model_id: str,
    bbox: BBox,
    *,
    start: datetime,
    end: datetime,
    cycle: str = "latest",
) -> WindField | None:
    """Load or fetch coarse NWP forcing at model-native resolution + hourly.

    Returns None on failure (caller reports 'unavailable'). No Open-Meteo fallback.
    Delta-extends the cached field when only some hours are missing.
    Raises ForecastRateLimitError when the upstream CDN rate-limits.

    This is the FORCING cache only — terrain downscaling is applied by
    WindProvider / DownscaledWindField on top.
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
    res_deg = _forcing_res_deg(model_id)
    try:
        lats_t, lons_t = canonical_grid(region, res_deg=res_deg)
    except ValueError as exc:
        logger.warning("canonical grid failed for %s: %s", model_id, exc)
        return None

    key = f"{model_id}|{region.key()}|{cyc.isoformat()}|{res_deg}"
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

        # Covering-region / resolution mismatches — regrid onto (lats_t, lons_t).
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
                "wind %s cache-hit region=%s hours=%d grid=%dx%d res=%.4f total=%.0fms",
                model_id,
                region.key(),
                len(needed),
                lats_t.size,
                lons_t.size,
                res_deg,
                (time.perf_counter() - t_all) * 1000,
            )
            return cached

        # Fetch only the native steps bracketing the missing hours.
        m_start = datetime.fromtimestamp(min(missing), UTC)
        m_end = datetime.fromtimestamp(max(missing), UTC)
        steps = src.steps_for_window(cyc, m_start, m_end)
        logger.info(
            "wind %s cache-miss region=%s missing=%d/%d steps=%d grid=%dx%d res=%.4f",
            model_id,
            region.key(),
            len(missing),
            len(needed),
            len(steps),
            lats_t.size,
            lons_t.size,
            res_deg,
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
                res_deg=res_deg,
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
):
    """Downscaled WindField for [start,end] — used by prepare + /route.

    Returns a DownscaledWindField when a terrain library is present, else the
    raw forcing WindField. Both expose wind_at(lat, lon, t).
    Raises ForecastRateLimitError; returns None if unavailable.
    """
    return get_provider().downscaled_field(model_id, bbox, start=start, end=end)


def get_display_series(
    model_id: str,
    bbox: BBox,
    *,
    start: datetime,
    end: datetime,
    zoom: float | None = None,
    enable_terrain: bool = True,
) -> dict | None:
    """Multi-hour series via WindProvider (zoom-tiered display window)."""
    return get_provider().display_series(
        model_id,
        bbox,
        start=start,
        end=end,
        zoom=zoom,
        enable_terrain=enable_terrain,
    )


def forecast_horizon_for_course(course) -> int:
    return routing_horizon_h(course)
