"""SQLite metadata catalog for cached WindFields (Zarr paths)."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import CACHE_DIR, WIND_TTL_S
from .metocean import WindField
from .schemas import BBox

_DB_PATH = CACHE_DIR / "catalog.db"
_lock = threading.Lock()


@dataclass
class CatalogRow:
    id: int
    model_id: str
    cycle_iso: str
    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float
    res_deg: float
    t_start: float
    t_end: float
    zarr_path: str
    created_at: float
    fresh_until: float


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wind_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    cycle_iso TEXT NOT NULL,
                    lat_min REAL NOT NULL,
                    lon_min REAL NOT NULL,
                    lat_max REAL NOT NULL,
                    lon_max REAL NOT NULL,
                    res_deg REAL NOT NULL,
                    t_start REAL NOT NULL,
                    t_end REAL NOT NULL,
                    zarr_path TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    fresh_until REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wind_model ON wind_fields(model_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wind_model_cycle "
                "ON wind_fields(model_id, cycle_iso)"
            )
            conn.commit()
        finally:
            conn.close()


def find_covering(
    model_id: str,
    bbox: BBox,
    *,
    start_epoch: float | None = None,
    end_epoch: float | None = None,
    min_t_end_epoch: float | None = None,
    now_epoch: float | None = None,
) -> CatalogRow | None:
    """Return the smallest fresh covering field for model/bbox/window.

    Prefer window-based coverage (t_start <= start AND t_end >= end). The legacy
    ``min_t_end_epoch`` kwarg is still accepted for older callers/tests.
    """
    now = now_epoch if now_epoch is not None else time.time()
    # Resolve window: prefer explicit start/end; fall back to min_t_end only.
    t_end = end_epoch if end_epoch is not None else min_t_end_epoch
    if t_end is None:
        t_end = now
    t_start = start_epoch if start_epoch is not None else 0.0

    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                SELECT * FROM wind_fields
                WHERE model_id = ?
                  AND lat_min <= ? AND lon_min <= ?
                  AND lat_max >= ? AND lon_max >= ?
                  AND t_start <= ?
                  AND t_end >= ?
                  AND fresh_until > ?
                ORDER BY ((lat_max - lat_min) * (lon_max - lon_min)) ASC
                LIMIT 1
                """,
                (
                    model_id,
                    bbox.lat_min,
                    bbox.lon_min,
                    bbox.lat_max,
                    bbox.lon_max,
                    t_start,
                    t_end,
                    now,
                ),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return CatalogRow(**dict(row))
        finally:
            conn.close()


def find_region_field(
    model_id: str,
    cycle_iso: str,
    bbox: BBox,
    *,
    now_epoch: float | None = None,
) -> CatalogRow | None:
    """Freshest row for this model+cycle whose region CONTAINS bbox (any time)."""
    now = now_epoch if now_epoch is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                SELECT * FROM wind_fields
                WHERE model_id = ?
                  AND cycle_iso = ?
                  AND lat_min <= ? AND lon_min <= ?
                  AND lat_max >= ? AND lon_max >= ?
                  AND fresh_until > ?
                ORDER BY ((lat_max - lat_min) * (lon_max - lon_min)) ASC
                LIMIT 1
                """,
                (
                    model_id,
                    cycle_iso,
                    bbox.lat_min,
                    bbox.lon_min,
                    bbox.lat_max,
                    bbox.lon_max,
                    now,
                ),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return CatalogRow(**dict(row))
        finally:
            conn.close()


def upsert(
    model_id: str,
    cycle: datetime,
    region: BBox,
    field: WindField,
    zarr_path: Path | str,
    ttl_s: float = WIND_TTL_S,
    *,
    res_deg: float | None = None,
) -> None:
    now = time.time()
    path = str(zarr_path)
    res = res_deg
    if res is None:
        if field.lats.size > 1:
            res = float(np_median_diff(field.lats))
        else:
            res = 0.25
    # Prefer actual field extent when available (covers subset writes).
    lat_min = float(field.lats[0]) if field.lats.size else region.lat_min
    lat_max = float(field.lats[-1]) if field.lats.size else region.lat_max
    lon_min = float(field.lons[0]) if field.lons.size else region.lon_min
    lon_max = float(field.lons[-1]) if field.lons.size else region.lon_max
    # Expand to at least the requested region so find_region_field still matches.
    lat_min = min(lat_min, region.lat_min)
    lon_min = min(lon_min, region.lon_min)
    lat_max = max(lat_max, region.lat_max)
    lon_max = max(lon_max, region.lon_max)

    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO wind_fields (
                    model_id, cycle_iso, lat_min, lon_min, lat_max, lon_max,
                    res_deg, t_start, t_end, zarr_path, created_at, fresh_until
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(zarr_path) DO UPDATE SET
                    lat_min=excluded.lat_min,
                    lon_min=excluded.lon_min,
                    lat_max=excluded.lat_max,
                    lon_max=excluded.lon_max,
                    res_deg=excluded.res_deg,
                    t_start=excluded.t_start,
                    t_end=excluded.t_end,
                    created_at=excluded.created_at,
                    fresh_until=excluded.fresh_until
                """,
                (
                    model_id,
                    cycle.isoformat(),
                    lat_min,
                    lon_min,
                    lat_max,
                    lon_max,
                    res,
                    float(field.times[0]),
                    float(field.times[-1]),
                    path,
                    now,
                    now + ttl_s,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def np_median_diff(arr) -> float:
    import numpy as np  # noqa: PLC0415

    d = np.diff(np.asarray(arr, dtype=float))
    return float(np.median(np.abs(d))) if d.size else 0.25


def prune(*, max_age_s: float = 7 * 24 * 3600) -> int:
    """Delete catalog rows older than max_age_s. Returns rows deleted."""
    cutoff = time.time() - max_age_s
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM wind_fields WHERE created_at < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount)
        finally:
            conn.close()
