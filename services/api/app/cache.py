"""Local disk cache for WindFields (local-first: fetch ashore, route offline)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .config import CACHE_DIR
from .metocean import WindField
from .schemas import BBox

_WIND_DIR = CACHE_DIR / "forecast"
_WIND_DIR.mkdir(parents=True, exist_ok=True)

WIND_TTL_S = 3 * 3600


def _bbox_hash(bbox: BBox) -> str:
    return hashlib.sha1(bbox.key().encode()).hexdigest()[:10]


def wind_key(
    model_id: str,
    bbox: BBox,
    cycle: str | None,
    *,
    dims: tuple[int, int] | None = None,
) -> str:
    base = f"{model_id}__{_bbox_hash(bbox)}__{cycle or 'latest'}"
    if dims is not None:
        return f"{base}__{dims[0]}x{dims[1]}"
    return base


def wind_path(key: str) -> Path:
    return _WIND_DIR / f"{key}.npz"


def has_wind(key: str) -> bool:
    return wind_path(key).exists()


def wind_is_fresh(key: str, ttl_s: float = WIND_TTL_S) -> bool:
    path = wind_path(key)
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < ttl_s


def save_wind(key: str, field: WindField) -> str:
    field.save_npz(wind_path(key))
    return key


def load_wind(key: str) -> WindField:
    path = wind_path(key)
    if not path.exists():
        raise KeyError(f"No cached WindField for key {key}")
    return WindField.load_npz(path)


def find_covering_wind(model_id: str, bbox: BBox) -> WindField | None:
    """Return any cached WindField for this model whose grid covers `bbox`."""
    prefix = f"{model_id}__"
    for path in sorted(_WIND_DIR.glob(f"{prefix}*.npz")):
        try:
            wf = WindField.load_npz(path)
        except Exception:  # noqa: BLE001
            continue
        if (
            wf.lats[0] <= bbox.lat_min
            and wf.lats[-1] >= bbox.lat_max
            and wf.lons[0] <= bbox.lon_min
            and wf.lons[-1] >= bbox.lon_max
        ):
            return wf
    return None


def find_overlapping_wind(model_id: str, bbox: BBox) -> WindField | None:
    """Return cached wind whose grid overlaps the query bbox (for map overlays)."""
    prefix = f"{model_id}__"
    best: WindField | None = None
    best_area = float("inf")
    for path in sorted(_WIND_DIR.glob(f"{prefix}*.npz")):
        try:
            wf = WindField.load_npz(path)
        except Exception:  # noqa: BLE001
            continue
        if (
            wf.lats[-1] < bbox.lat_min
            or wf.lats[0] > bbox.lat_max
            or wf.lons[-1] < bbox.lon_min
            or wf.lons[0] > bbox.lon_max
        ):
            continue
        area = (wf.lats[-1] - wf.lats[0]) * (wf.lons[-1] - wf.lons[0])
        if area < best_area:
            best_area = area
            best = wf
    return best
