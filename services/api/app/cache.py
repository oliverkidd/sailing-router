"""Local disk cache for metocean fields (local-first: fetch ashore, route offline).

WindFields are cached to .npz keyed by (model_id, bbox_hash, cycle). CurrentFields
(M5) reuse the same directory layout. TODO(v2): NetCDF/zarr for interoperability.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import CACHE_DIR
from .metocean import CurrentField, WindField
from .schemas import BBox

_WIND_DIR = CACHE_DIR / "forecast"
_CURRENT_DIR = CACHE_DIR / "currents"
_WIND_DIR.mkdir(parents=True, exist_ok=True)
_CURRENT_DIR.mkdir(parents=True, exist_ok=True)


def _bbox_hash(bbox: BBox) -> str:
    return hashlib.sha1(bbox.key().encode()).hexdigest()[:10]


def wind_key(model_id: str, bbox: BBox, cycle: str | None) -> str:
    return f"{model_id}__{_bbox_hash(bbox)}__{cycle or 'latest'}"


def wind_path(key: str) -> Path:
    return _WIND_DIR / f"{key}.npz"


def has_wind(key: str) -> bool:
    return wind_path(key).exists()


def save_wind(key: str, field: WindField) -> str:
    field.save_npz(wind_path(key))
    return key


def load_wind(key: str) -> WindField:
    path = wind_path(key)
    if not path.exists():
        raise KeyError(f"No cached WindField for key {key}")
    return WindField.load_npz(path)


def current_key(source: str, bbox: BBox, window_start: str) -> str:
    return f"{source}__{_bbox_hash(bbox)}__{window_start}"


def current_path(key: str) -> Path:
    return _CURRENT_DIR / f"{key}.npz"


def has_current(key: str) -> bool:
    return current_path(key).exists()


def save_current(key: str, field: CurrentField) -> str:
    field.save_npz(current_path(key))
    return key


def load_current(key: str) -> CurrentField:
    path = current_path(key)
    if not path.exists():
        raise KeyError(f"No cached CurrentField for key {key}")
    return CurrentField.load_npz(path)
