"""Loader for the offline WindNinja terrain-response library.

Library layout (per tile Zarr under data/cache/wind_terrain/):
  speedup[sector, daynight, lat, lon]
  deflection_deg[sector, daynight, lat, lon]
  optionally katabatic_u / katabatic_v

When no library is present the loader returns None and the operator is identity.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import CACHE_DIR, DATA_DIR
from ..schemas import BBox
from .zoom_tiers import WIND_CANON_RES_DEG

logger = logging.getLogger(__name__)

OPERATOR_VERSION = "v1"
LIBRARY_VERSION = "wn2"  # bump when WindNinja bake params / roughness change

TERRAIN_DIR = Path(
    __import__("os").environ.get(
        "SAILING_WIND_TERRAIN_DIR",
        str(CACHE_DIR / "wind_terrain"),
    )
)
# Optional region-wide mosaic (preferred when present).
REGION_LIBRARY = TERRAIN_DIR / "region" / "library.zarr"

N_SECTORS = 16
SECTOR_DEG = 360.0 / N_SECTORS


@dataclass
class TerrainResponse:
    """Point sample from the terrain-response library."""

    speedup: float = 1.0
    deflection_deg: float = 0.0
    katabatic_u: float = 0.0
    katabatic_v: float = 0.0


@dataclass
class TerrainLibrary:
    """In-memory window of the terrain-response library."""

    lats: np.ndarray  # (nlat,) ascending
    lons: np.ndarray  # (nlon,) ascending
    # (n_sectors, 2, nlat, nlon) — axis 1: 0=day, 1=night
    speedup: np.ndarray
    deflection_deg: np.ndarray
    katabatic_u: np.ndarray | None = None
    katabatic_v: np.ndarray | None = None
    version: str = LIBRARY_VERSION
    source: str = "unknown"  # windninja | identity | …

    @property
    def available(self) -> bool:
        return self.lats.size >= 2 and self.lons.size >= 2

    def sample(
        self,
        lat: float,
        lon: float,
        *,
        wind_dir_from_deg: float,
        day_frac: float,
    ) -> TerrainResponse:
        """Bilinear spatial sample + sector/day-night blend."""
        if not self.available:
            return TerrainResponse()
        if (
            lat < float(self.lats[0])
            or lat > float(self.lats[-1])
            or lon < float(self.lons[0])
            or lon > float(self.lons[-1])
        ):
            return TerrainResponse()

        s0, s1, ws = _sector_weights(wind_dir_from_deg)
        wd = float(np.clip(day_frac, 0.0, 1.0))
        wn = 1.0 - wd
        i, j, fx, fy = self._indices(lat, lon)

        def blend(cube: np.ndarray) -> float:
            c00 = self._bilinear_at(cube[s0, 0], i, j, fx, fy)
            c10 = self._bilinear_at(cube[s1, 0], i, j, fx, fy)
            c01 = self._bilinear_at(cube[s0, 1], i, j, fx, fy)
            c11 = self._bilinear_at(cube[s1, 1], i, j, fx, fy)
            day = ws * c00 + (1 - ws) * c10
            night = ws * c01 + (1 - ws) * c11
            return wd * day + wn * night

        su = blend(self.speedup)
        de = blend(self.deflection_deg)
        ku = kv = 0.0
        if self.katabatic_u is not None and self.katabatic_v is not None:
            ku = blend(self.katabatic_u)
            kv = blend(self.katabatic_v)
        return TerrainResponse(
            speedup=float(su),
            deflection_deg=float(de),
            katabatic_u=float(ku),
            katabatic_v=float(kv),
        )

    def _indices(self, lat: float, lon: float) -> tuple[int, int, float, float]:
        la = float(np.clip(lat, self.lats[0], self.lats[-1]))
        lo = float(np.clip(lon, self.lons[0], self.lons[-1]))
        i = int(np.clip(np.searchsorted(self.lats, la) - 1, 0, self.lats.size - 2))
        j = int(np.clip(np.searchsorted(self.lons, lo) - 1, 0, self.lons.size - 2))
        la0, la1 = float(self.lats[i]), float(self.lats[i + 1])
        lo0, lo1 = float(self.lons[j]), float(self.lons[j + 1])
        fy = 0.0 if la1 == la0 else (la - la0) / (la1 - la0)
        fx = 0.0 if lo1 == lo0 else (lo - lo0) / (lo1 - lo0)
        return i, j, fx, fy

    def _bilinear_at(
        self, arr2d: np.ndarray, i: int, j: int, fx: float, fy: float
    ) -> float:
        v0 = float(arr2d[i, j]) * (1 - fx) + float(arr2d[i, j + 1]) * fx
        v1 = float(arr2d[i + 1, j]) * (1 - fx) + float(arr2d[i + 1, j + 1]) * fx
        return v0 * (1 - fy) + v1 * fy

    def _bilinear(self, arr2d: np.ndarray, lat: float, lon: float) -> float:
        i, j, fx, fy = self._indices(lat, lon)
        return self._bilinear_at(arr2d, i, j, fx, fy)


def _sector_weights(wind_dir_from_deg: float) -> tuple[int, int, float]:
    """Two bracketing sectors + weight toward s0 (sector centres at 0, 22.5, ...)."""
    d = float(wind_dir_from_deg) % 360.0
    # Sector centres at SECTOR_DEG * i.
    idx = d / SECTOR_DEG
    s0 = int(math.floor(idx)) % N_SECTORS
    s1 = (s0 + 1) % N_SECTORS
    frac = idx - math.floor(idx)
    return s0, s1, 1.0 - frac


def library_available() -> bool:
    return REGION_LIBRARY.exists() or any(TERRAIN_DIR.glob("tile_*/library.zarr"))


# Process-local LRU of loaded TerrainLibrary windows.
_LIB_LRU_MAX = 6
_LIB_TTL_S = 1800
_lib_lru: OrderedDict[str, tuple[float, TerrainLibrary]] = OrderedDict()
_lib_guard = threading.Lock()


def _lib_cache_get(key: str) -> TerrainLibrary | None:
    with _lib_guard:
        hit = _lib_lru.get(key)
        if hit is None:
            return None
        ts, lib = hit
        if time.time() - ts > _LIB_TTL_S:
            del _lib_lru[key]
            return None
        _lib_lru.move_to_end(key)
        return lib


def _lib_cache_put(key: str, lib: TerrainLibrary) -> None:
    with _lib_guard:
        _lib_lru[key] = (time.time(), lib)
        _lib_lru.move_to_end(key)
        while len(_lib_lru) > _LIB_LRU_MAX:
            _lib_lru.popitem(last=False)


def load_library_for_bbox(bbox: BBox, *, overview: int = 1) -> TerrainLibrary | None:
    """Load (or window) the terrain library covering bbox.

    ``overview`` is a power-of-2 downsample factor (1 = native 200 m).
    Returns None when no library has been baked offline.
    """
    cache_key = f"{bbox.key()}|o{max(1, int(overview))}|{LIBRARY_VERSION}"
    hit = _lib_cache_get(cache_key)
    if hit is not None:
        return hit

    lib: TerrainLibrary | None = None
    if REGION_LIBRARY.exists():
        try:
            lib = _load_zarr_window(REGION_LIBRARY, bbox, overview=overview)
        except Exception as exc:  # noqa: BLE001
            logger.warning("terrain library load failed: %s", exc)
            lib = None
    else:
        tiles = sorted(TERRAIN_DIR.glob("tile_*/library.zarr"))
        for tile in tiles:
            try:
                candidate = _load_zarr_window(tile, bbox, overview=overview)
                if candidate is not None and candidate.available:
                    lib = candidate
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip terrain tile %s: %s", tile, exc)

    if lib is not None and lib.available:
        # Prefer real library version in cache key on next hit via stored attrs.
        _lib_cache_put(cache_key, lib)
        return lib
    return None


def _load_zarr_window(
    path: Path, bbox: BBox, *, overview: int = 1
) -> TerrainLibrary | None:
    import xarray as xr  # noqa: PLC0415

    ds = xr.open_zarr(path)
    try:
        lats = np.asarray(ds["lat"].values, dtype=float)
        lons = np.asarray(ds["lon"].values, dtype=float)
        if lats.size < 2 or lons.size < 2:
            return None

        i0 = max(0, int(np.searchsorted(lats, bbox.lat_min) - 1))
        i1 = min(lats.size, int(np.searchsorted(lats, bbox.lat_max)) + 1)
        j0 = max(0, int(np.searchsorted(lons, bbox.lon_min) - 1))
        j1 = min(lons.size, int(np.searchsorted(lons, bbox.lon_max)) + 1)
        i1 = max(i1, i0 + 2)
        j1 = max(j1, j0 + 2)

        step = max(1, int(overview))
        la = lats[i0:i1:step]
        lo = lons[j0:j1:step]
        su = np.asarray(
            ds["speedup"].values[:, :, i0:i1:step, j0:j1:step], dtype=np.float32
        )
        de = np.asarray(
            ds["deflection_deg"].values[:, :, i0:i1:step, j0:j1:step], dtype=np.float32
        )
        ku = kv = None
        if "katabatic_u" in ds and "katabatic_v" in ds:
            ku = np.asarray(
                ds["katabatic_u"].values[:, :, i0:i1:step, j0:j1:step], dtype=np.float32
            )
            kv = np.asarray(
                ds["katabatic_v"].values[:, :, i0:i1:step, j0:j1:step], dtype=np.float32
            )
        ver = str(ds.attrs.get("version", LIBRARY_VERSION))
        src = str(ds.attrs.get("source", "unknown"))
        return TerrainLibrary(
            lats=la,
            lons=lo,
            speedup=su,
            deflection_deg=de,
            katabatic_u=ku,
            katabatic_v=kv,
            version=ver,
            source=src,
        )
    finally:
        ds.close()


def write_identity_library(path: Path, bbox: BBox, res_deg: float = WIND_CANON_RES_DEG) -> Path:
    """Write a unit-speedup library (for tests / dry-run without WindNinja)."""
    import xarray as xr  # noqa: PLC0415

    lat0 = math.floor(bbox.lat_min / res_deg) * res_deg
    lon0 = math.floor(bbox.lon_min / res_deg) * res_deg
    nlat = max(2, int(round((bbox.lat_max - lat0) / res_deg)) + 1)
    nlon = max(2, int(round((bbox.lon_max - lon0) / res_deg)) + 1)
    lats = lat0 + np.arange(nlat, dtype=float) * res_deg
    lons = lon0 + np.arange(nlon, dtype=float) * res_deg
    shape = (N_SECTORS, 2, nlat, nlon)

    path = Path(path)
    if path.exists():
        import shutil  # noqa: PLC0415

        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = xr.Dataset(
        data_vars={
            "speedup": (
                ("sector", "daynight", "lat", "lon"),
                np.ones(shape, dtype=np.float32),
            ),
            "deflection_deg": (
                ("sector", "daynight", "lat", "lon"),
                np.zeros(shape, dtype=np.float32),
            ),
        },
        coords={
            "sector": np.arange(N_SECTORS),
            "daynight": np.array([0, 1]),
            "lat": lats,
            "lon": lons,
        },
        attrs={
            "version": LIBRARY_VERSION,
            "res_deg": float(res_deg),
            "source": "identity",
            "dem": str(DATA_DIR / "dem"),
        },
    )
    ds.to_zarr(path, mode="w", zarr_format=2)
    return path


def day_frac_utc(epoch_s: float, lon: float) -> float:
    """Rough local day fraction in [0,1] from UTC epoch + longitude (1=noon)."""
    hours = (epoch_s / 3600.0 + lon / 15.0) % 24.0
    return float(0.5 * (1.0 + math.cos(math.radians((hours - 12.0) * 15.0))))
