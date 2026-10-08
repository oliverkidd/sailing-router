"""Single wind entry point for display + routing.

``WindProvider`` loads coarse NWP forcing, optionally attaches the offline
terrain-response library, and returns a WindField-compatible object so the
hardened router needs no changes.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any

import numpy as np

from ..metocean import WindField
from ..overlays.grid import display_series_payload
from ..schemas import BBox
from ..util.bbox import budget_dims
from . import operator as op
from .downscaled import DownscaledWindField
from .terrain_library import (
    LIBRARY_VERSION,
    library_available,
    library_meta_for_bbox,
    load_library_for_bbox,
)
from .zoom_tiers import WIND_CANON_RES_DEG, display_window, tier_for_zoom

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]

_provider: WindProvider | None = None

# Short-TTL LRU of materialised per-hour display grids.
# Keyed by window+grid+hour (not the request [start,end]) so scrubbing
# reuses hours already downscaled for an overlapping prefetch window.
_WINDOW_LRU_MAX = 96
_WINDOW_TTL_S = 900
_window_lru: OrderedDict[str, tuple[float, WindField]] = OrderedDict()
_window_guard = threading.Lock()


def _window_get(key: str) -> WindField | None:
    with _window_guard:
        hit = _window_lru.get(key)
        if hit is None:
            return None
        ts, field = hit
        if time.time() - ts > _WINDOW_TTL_S:
            del _window_lru[key]
            return None
        _window_lru.move_to_end(key)
        return field


def _window_put(key: str, field: WindField) -> None:
    with _window_guard:
        _window_lru[key] = (time.time(), field)
        _window_lru.move_to_end(key)
        while len(_window_lru) > _WINDOW_LRU_MAX:
            _window_lru.popitem(last=False)


def _hour_cache_key(base: str, hour_epoch: float) -> str:
    return f"{base}|h{int(round(hour_epoch))}"


def _slice_field_times(
    field: DownscaledWindField | WindField, indices: list[int]
) -> DownscaledWindField | WindField:
    """Return a field containing only the selected time indices."""
    if not indices:
        raise ValueError("indices must be non-empty")
    idx = np.asarray(indices, dtype=int)
    if isinstance(field, DownscaledWindField):
        forcing = field.forcing
        sliced = WindField.build(
            forcing.model_id,
            forcing.times[idx],
            forcing.lats,
            forcing.lons,
            forcing.u10[idx],
            forcing.v10[idx],
        )
        return DownscaledWindField(
            forcing=sliced,
            library=field.library,
            operator_version=field.operator_version,
        )
    return WindField.build(
        field.model_id,
        field.times[idx],
        field.lats,
        field.lons,
        field.u10[idx],
        field.v10[idx],
    )


def _emit(on_progress: ProgressCallback | None, event: dict[str, Any]) -> None:
    if event.get("type") == "stage":
        logger.info(
            "wind stage %s %s ms=%s cache_hit=%s terrain_library=%s",
            event.get("key"),
            event.get("status"),
            event.get("ms"),
            event.get("cache_hit"),
            event.get("terrain_library"),
        )
    if on_progress is not None:
        on_progress(event)


class WindProvider:
    """Factory for downscaled wind used by /wind/area, /route, /prepare."""

    def __init__(self, *, enable_terrain: bool = True) -> None:
        self.enable_terrain = enable_terrain

    def downscaled_field(
        self,
        model_id: str,
        bbox: BBox,
        *,
        start: datetime,
        end: datetime,
        enable_terrain: bool | None = None,
        overview: int = 1,
        on_progress: ProgressCallback | None = None,
        pad_region: bool = True,
    ) -> DownscaledWindField | WindField | None:
        """Forcing + optional terrain library, WindField-compatible."""
        from ..field_service import get_area_field  # noqa: PLC0415

        _emit(on_progress, {"type": "stage", "key": "forcing", "status": "start"})
        t0 = time.perf_counter()
        forcing = get_area_field(
            model_id, bbox, start=start, end=end, pad_region=pad_region
        )
        _emit(
            on_progress,
            {
                "type": "stage",
                "key": "forcing",
                "status": "done",
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        )
        if forcing is None:
            return None

        use_terrain = self.enable_terrain if enable_terrain is None else enable_terrain
        library = None
        _emit(on_progress, {"type": "stage", "key": "library", "status": "start"})
        t1 = time.perf_counter()
        if use_terrain and library_available():
            try:
                library = load_library_for_bbox(bbox, overview=max(1, int(overview)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("terrain library attach failed: %s", exc)
                library = None
        lib_label = (
            "none"
            if library is None
            else (library.source or "unknown")
        )
        _emit(
            on_progress,
            {
                "type": "stage",
                "key": "library",
                "status": "done",
                "ms": round((time.perf_counter() - t1) * 1000, 1),
                "terrain_library": lib_label,
            },
        )
        if library is None:
            return forcing
        return DownscaledWindField(forcing=forcing, library=library)

    def display_series(
        self,
        model_id: str,
        viewport: BBox,
        *,
        start: datetime,
        end: datetime,
        zoom: float | None = None,
        enable_terrain: bool = True,
        corridor: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> dict | None:
        """Multi-hour display payload at the zoom-tier resolution/window.

        corridor=True: treat viewport as an exact course corridor — no browse
        window pad, no region_bbox snap. Uses fine display density.
        """
        if corridor:
            # Fine tier density without coarse +2° pad (zoom=None → coarse).
            tier = tier_for_zoom(11.0)
            window = BBox(
                lat_min=round(viewport.lat_min, 4),
                lon_min=round(viewport.lon_min, 4),
                lat_max=round(viewport.lat_max, 4),
                lon_max=round(viewport.lon_max, 4),
            )
            overview = overview_for_zoom(11.0)
            pad_region = False
        else:
            tier = tier_for_zoom(zoom)
            window = display_window(viewport, zoom)
            overview = overview_for_zoom(zoom)
            pad_region = True
        field = self.downscaled_field(
            model_id,
            window,
            start=start,
            end=end,
            enable_terrain=enable_terrain,
            overview=overview,
            on_progress=on_progress,
            pad_region=pad_region,
        )
        if field is None:
            return None

        nlat, nlon = budget_dims(window, tier.target_res_deg, max_pts=tier.max_pts)
        lats = np.linspace(window.lat_min, window.lat_max, nlat)
        lons = np.linspace(window.lon_min, window.lon_max, nlon)

        lib_ver = LIBRARY_VERSION
        terrain_library = "none"
        if isinstance(field, DownscaledWindField) and field.library is not None:
            lib_ver = field.library.version
            src = (field.library.source or "").lower()
            if src == "windninja":
                terrain_library = "windninja"
            elif src == "identity":
                terrain_library = "identity"
            else:
                terrain_library = src or "unknown"
        elif enable_terrain and not library_available():
            terrain_library = "none"
        elif not enable_terrain:
            if library_available():
                try:
                    peek = library_meta_for_bbox(window)
                    if peek is not None:
                        src_raw, lib_ver = peek
                        src = (src_raw or "").lower()
                        terrain_library = (
                            "windninja"
                            if src == "windninja"
                            else "identity"
                            if src == "identity"
                            else src or "unknown"
                        )
                except Exception:  # noqa: BLE001
                    terrain_library = "none"
            else:
                terrain_library = "none"

        terrain_tag = "t1" if enable_terrain else "t0"
        corridor_tag = "c1" if corridor else "c0"
        base_key = (
            f"{model_id}|{window.key()}|{nlat}x{nlon}|{lib_ver}|"
            f"{op.OPERATOR_VERSION}|{tier.name}|{terrain_tag}|o{overview}|"
            f"{corridor_tag}"
        )
        start_epoch = start.timestamp()
        end_epoch = end.timestamp()

        # Hours present on the forcing field that fall inside the request window.
        needed: list[tuple[int, float]] = []
        for ki, te in enumerate(field.times):
            te_f = float(te)
            if start_epoch - 1 <= te_f <= end_epoch + 1:
                needed.append((ki, te_f))

        _emit(on_progress, {"type": "stage", "key": "regrid", "status": "start"})
        t2 = time.perf_counter()
        cached_hours: dict[float, WindField] = {}
        miss_indices: list[int] = []
        miss_epochs: list[float] = []
        for ki, te_f in needed:
            hit = _window_get(_hour_cache_key(base_key, te_f))
            if hit is not None:
                cached_hours[te_f] = hit
            else:
                miss_indices.append(ki)
                miss_epochs.append(te_f)

        if miss_indices:
            sliced = _slice_field_times(field, miss_indices)
            fresh = sliced.regrid(lats, lons)
            for mi, te_f in enumerate(miss_epochs):
                single = WindField.build(
                    fresh.model_id,
                    fresh.times[mi : mi + 1],
                    fresh.lats,
                    fresh.lons,
                    fresh.u10[mi : mi + 1],
                    fresh.v10[mi : mi + 1],
                )
                _window_put(_hour_cache_key(base_key, te_f), single)
                cached_hours[te_f] = single

        hour_fields = [cached_hours[te_f] for _, te_f in needed]
        hits = len(needed) - len(miss_indices)

        if not hour_fields:
            # No overlapping hours — fall back to a full regrid of whatever we have.
            grid = field.regrid(lats, lons)
            cache_hit = False
        else:
            grid = (
                hour_fields[0]
                if len(hour_fields) == 1
                else WindField.concat_time(hour_fields)
            )
            cache_hit = hits == len(hour_fields)
        _emit(
            on_progress,
            {
                "type": "stage",
                "key": "regrid",
                "status": "done",
                "ms": round((time.perf_counter() - t2) * 1000, 1),
                "cache_hit": cache_hit,
                "hours_hit": hits,
                "hours_miss": len(miss_indices),
            },
        )

        _emit(on_progress, {"type": "stage", "key": "payload", "status": "start"})
        t3 = time.perf_counter()
        payload = display_series_payload(grid, window, nlat, nlon, start, end)
        payload["zoom_tier"] = tier.name
        payload["target_res_deg"] = tier.target_res_deg
        payload["terrain_applied"] = bool(
            enable_terrain
            and isinstance(field, DownscaledWindField)
            and field.library is not None
        )
        payload["terrain_library"] = terrain_library
        payload["terrain_library_version"] = lib_ver
        _emit(
            on_progress,
            {
                "type": "stage",
                "key": "payload",
                "status": "done",
                "ms": round((time.perf_counter() - t3) * 1000, 1),
            },
        )
        return payload


def get_provider() -> WindProvider:
    global _provider
    if _provider is None:
        _provider = WindProvider()
    return _provider


def overview_for_zoom(zoom: float | None) -> int:
    """Library overview factor (power of 2) matching display density."""
    tier = tier_for_zoom(zoom)
    ratio = max(1.0, tier.target_res_deg / WIND_CANON_RES_DEG)
    return max(1, 2 ** int(round(math.log2(ratio))))
