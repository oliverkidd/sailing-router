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
    load_library_for_bbox,
)
from .zoom_tiers import WIND_CANON_RES_DEG, display_window, tier_for_zoom

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]

_provider: WindProvider | None = None

# Short-TTL LRU of materialised display grids.
_WINDOW_LRU_MAX = 24
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


def _emit(on_progress: ProgressCallback | None, event: dict[str, Any]) -> None:
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
        on_progress: ProgressCallback | None = None,
    ) -> dict | None:
        """Multi-hour display payload at the zoom-tier resolution/window."""
        tier = tier_for_zoom(zoom)
        window = display_window(viewport, zoom)
        overview = overview_for_zoom(zoom)
        field = self.downscaled_field(
            model_id,
            window,
            start=start,
            end=end,
            enable_terrain=enable_terrain,
            overview=overview,
            on_progress=on_progress,
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
                    peek = load_library_for_bbox(window, overview=max(16, overview))
                    if peek is not None:
                        src = (peek.source or "").lower()
                        terrain_library = (
                            "windninja"
                            if src == "windninja"
                            else "identity"
                            if src == "identity"
                            else src or "unknown"
                        )
                        lib_ver = peek.version
                except Exception:  # noqa: BLE001
                    terrain_library = "none"
            else:
                terrain_library = "none"

        terrain_tag = "t1" if enable_terrain else "t0"
        cache_key = (
            f"{model_id}|{window.key()}|{nlat}x{nlon}|{start.isoformat()}|"
            f"{end.isoformat()}|{lib_ver}|{op.OPERATOR_VERSION}|{tier.name}|"
            f"{terrain_tag}|o{overview}"
        )
        _emit(on_progress, {"type": "stage", "key": "regrid", "status": "start"})
        t2 = time.perf_counter()
        grid = _window_get(cache_key)
        cache_hit = grid is not None
        if grid is None:
            grid = field.regrid(lats, lons)
            _window_put(cache_key, grid)
        _emit(
            on_progress,
            {
                "type": "stage",
                "key": "regrid",
                "status": "done",
                "ms": round((time.perf_counter() - t2) * 1000, 1),
                "cache_hit": cache_hit,
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
