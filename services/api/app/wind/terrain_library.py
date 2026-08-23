"""Loader for the offline WindNinja terrain-response library.

Library layout (per tile Zarr under data/cache/wind_terrain/):
  speedup[sector, daynight, lat, lon]
  deflection_deg[sector, daynight, lat, lon]
  optionally katabatic_u / katabatic_v

When no library is present the loader returns None and the operator is identity.

At load time land cells are forced to identity (sea-only adjustments) and
power-of-2 overview pyramids are block-mean downsampled to avoid stride aliasing.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import CACHE_DIR, DATA_DIR
from ..schemas import BBox
from .zoom_tiers import WIND_CANON_RES_DEG

logger = logging.getLogger(__name__)

OPERATOR_VERSION = "v1"
# wn3-sea: sea-only mask + block-mean overviews + identity deadband + deflection clamp.
LIBRARY_VERSION = "wn3-sea"

# Snap near-identity cells to exact identity (kills uniform ~0.998 far-field offset).
_SPEEDUP_IDENTITY_EPS = 0.005
_DEFLECTION_IDENTITY_EPS_DEG = 0.5
# Safety clamp so wrap-corrupt bake artifacts cannot flip wind direction.
_DEFLECTION_MAX_ABS_DEG = 45.0

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
_OVERVIEW_LEVELS = (2, 4, 8, 16, 32)


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


@dataclass
class _FullLibraryPack:
    """Native-resolution library + block-mean overview pyramids."""

    base: TerrainLibrary
    overviews: dict[int, TerrainLibrary] = field(default_factory=dict)


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


# Process-local cache of fully decoded region/tile libraries (path + mtime).
_FULL_LRU_MAX = 2
_full_lru: OrderedDict[str, tuple[float, _FullLibraryPack]] = OrderedDict()
_full_guard = threading.Lock()

# Windowed views derived from a full library (bbox + overview).
_LIB_LRU_MAX = 12
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


def _zarr_mtime(path: Path) -> float:
    """Stable mtime for a Zarr store (dir or .zattrs file)."""
    try:
        zattrs = path / ".zattrs"
        if zattrs.exists():
            return zattrs.stat().st_mtime
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _full_cache_key(path: Path) -> str:
    return f"{path.resolve()}|{_zarr_mtime(path)}|{LIBRARY_VERSION}"


def _full_cache_get(path: Path) -> _FullLibraryPack | None:
    key = _full_cache_key(path)
    with _full_guard:
        hit = _full_lru.get(key)
        if hit is None:
            # Drop stale entries for the same path under a different mtime/version.
            prefix = f"{path.resolve()}|"
            stale = [k for k in _full_lru if k.startswith(prefix)]
            for k in stale:
                del _full_lru[k]
            return None
        _full_lru.move_to_end(key)
        return hit[1]


def _full_cache_put(path: Path, pack: _FullLibraryPack) -> None:
    key = _full_cache_key(path)
    with _full_guard:
        prefix = f"{path.resolve()}|"
        stale = [k for k in _full_lru if k.startswith(prefix) and k != key]
        for k in stale:
            del _full_lru[k]
        _full_lru[key] = (time.time(), pack)
        _full_lru.move_to_end(key)
        while len(_full_lru) > _FULL_LRU_MAX:
            _full_lru.popitem(last=False)


def library_meta_for_bbox(bbox: BBox) -> tuple[str, str] | None:
    """Return (source, version) for a covering library without decoding arrays.

    Used by the terrain=false metadata peek so we do not pay a Zarr decode.
    Reads only attrs + lat/lon coords to test intersection. Version is the
    runtime LIBRARY_VERSION (sea-mask applied at load).
    """
    paths: list[Path] = []
    if REGION_LIBRARY.exists():
        paths.append(REGION_LIBRARY)
    else:
        paths.extend(sorted(TERRAIN_DIR.glob("tile_*/library.zarr")))

    for path in paths:
        try:
            import xarray as xr  # noqa: PLC0415

            ds = xr.open_zarr(path)
            try:
                lats = np.asarray(ds["lat"].values, dtype=float)
                lons = np.asarray(ds["lon"].values, dtype=float)
                if lats.size < 2 or lons.size < 2:
                    continue
                if (
                    bbox.lat_max < float(lats[0])
                    or bbox.lat_min > float(lats[-1])
                    or bbox.lon_max < float(lons[0])
                    or bbox.lon_min > float(lons[-1])
                ):
                    if path == REGION_LIBRARY:
                        return None
                    continue
                src = str(ds.attrs.get("source", "unknown"))
                return src, LIBRARY_VERSION
            finally:
                ds.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("terrain meta peek failed for %s: %s", path, exc)
    return None


def _bbox_intersects_library(bbox: BBox, lib: TerrainLibrary) -> bool:
    if not lib.available:
        return False
    return not (
        bbox.lat_max < float(lib.lats[0])
        or bbox.lat_min > float(lib.lats[-1])
        or bbox.lon_max < float(lib.lons[0])
        or bbox.lon_min > float(lib.lons[-1])
    )


def load_library_for_bbox(bbox: BBox, *, overview: int = 1) -> TerrainLibrary | None:
    """Load (or window) the terrain library covering bbox.

    ``overview`` is a power-of-2 downsample factor (1 = native 200 m).
    Returns None when no library has been baked offline, or when bbox lies
    entirely outside every available library footprint.
    """
    step = max(1, int(overview))
    cache_key = f"{bbox.key()}|o{step}|{LIBRARY_VERSION}"
    hit = _lib_cache_get(cache_key)
    if hit is not None:
        return hit

    lib: TerrainLibrary | None = None
    if REGION_LIBRARY.exists():
        try:
            lib = _window_from_path(REGION_LIBRARY, bbox, overview=step)
        except Exception as exc:  # noqa: BLE001
            logger.warning("terrain library load failed: %s", exc)
            lib = None
    else:
        tiles = sorted(TERRAIN_DIR.glob("tile_*/library.zarr"))
        for tile in tiles:
            try:
                candidate = _window_from_path(tile, bbox, overview=step)
                if candidate is not None and candidate.available:
                    lib = candidate
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip terrain tile %s: %s", tile, exc)

    if lib is not None and lib.available:
        _lib_cache_put(cache_key, lib)
        return lib
    return None


def _window_from_path(
    path: Path, bbox: BBox, *, overview: int = 1
) -> TerrainLibrary | None:
    pack = _load_full_library(path)
    if pack is None or not pack.base.available:
        return None
    return _window_library(pack, bbox, overview=overview)


def _sea_mask_sidecar(path: Path, nlat: int, nlon: int) -> Path:
    return path.parent / f"sea_mask_{LIBRARY_VERSION}_{nlat}x{nlon}.npz"


def _load_or_build_land_mask(
    path: Path, lats: np.ndarray, lons: np.ndarray
) -> np.ndarray | None:
    """Boolean land mask on (nlat, nlon); cached as sidecar npz next to the Zarr."""
    from .roughness import _land_mask_raster  # noqa: PLC0415

    nlat, nlon = int(lats.size), int(lons.size)
    sidecar = _sea_mask_sidecar(path, nlat, nlon)
    if sidecar.exists():
        try:
            data = np.load(sidecar)
            mask = np.asarray(data["land"], dtype=bool)
            if mask.shape == (nlat, nlon):
                return mask
        except Exception as exc:  # noqa: BLE001
            logger.debug("sea-mask sidecar read failed: %s", exc)

    bbox = BBox(
        lat_min=float(lats[0]),
        lon_min=float(lons[0]),
        lat_max=float(lats[-1]),
        lon_max=float(lons[-1]),
    )
    t0 = time.perf_counter()
    land = _land_mask_raster(bbox, lats, lons)
    if land is None:
        logger.warning(
            "sea-mask: no land chart for %s — leaving baked land response intact",
            path.name,
        )
        return None
    land = np.asarray(land, dtype=bool)
    try:
        np.savez_compressed(sidecar, land=land.astype(np.uint8))
    except Exception as exc:  # noqa: BLE001
        logger.debug("sea-mask sidecar write failed: %s", exc)
    logger.info(
        "sea-mask rasterized %s land=%d/%d %.0fms",
        path.name,
        int(land.sum()),
        land.size,
        (time.perf_counter() - t0) * 1000,
    )
    return land


def _apply_sea_only_mask(
    su: np.ndarray,
    de: np.ndarray,
    ku: np.ndarray | None,
    kv: np.ndarray | None,
    land: np.ndarray,
) -> None:
    """In-place: land cells → identity (speedup=1, deflection=0, katabatic=0)."""
    su[:, :, land] = 1.0
    de[:, :, land] = 0.0
    if ku is not None:
        ku[:, :, land] = 0.0
    if kv is not None:
        kv[:, :, land] = 0.0


def _apply_identity_deadband_and_clamp(su: np.ndarray, de: np.ndarray) -> None:
    """In-place hygiene: snap near-identity + clamp extreme deflection."""
    su[np.abs(su - 1.0) <= _SPEEDUP_IDENTITY_EPS] = 1.0
    de[np.abs(de) <= _DEFLECTION_IDENTITY_EPS_DEG] = 0.0
    np.clip(de, -_DEFLECTION_MAX_ABS_DEG, _DEFLECTION_MAX_ABS_DEG, out=de)


def _block_mean_spatial(arr: np.ndarray, step: int) -> np.ndarray:
    """Block-mean the last two axes by ``step`` (truncate remainder)."""
    if step <= 1:
        return arr
    nlat, nlon = arr.shape[-2], arr.shape[-1]
    nlat_o = nlat // step
    nlon_o = nlon // step
    if nlat_o < 2 or nlon_o < 2:
        return arr
    trimmed = arr[..., : nlat_o * step, : nlon_o * step]
    leading = trimmed.shape[:-2]
    reshaped = trimmed.reshape(*leading, nlat_o, step, nlon_o, step)
    return reshaped.mean(axis=(-3, -1)).astype(np.float32, copy=False)


def _block_mean_deflection(de: np.ndarray, step: int) -> np.ndarray:
    """Circular block-mean of deflection angles (degrees)."""
    if step <= 1:
        return de
    rad = np.radians(de.astype(np.float64))
    sin_m = _block_mean_spatial(np.sin(rad).astype(np.float32), step)
    cos_m = _block_mean_spatial(np.cos(rad).astype(np.float32), step)
    return np.degrees(np.arctan2(sin_m, cos_m)).astype(np.float32)


def _downsample_axes(lats: np.ndarray, lons: np.ndarray, step: int) -> tuple[np.ndarray, np.ndarray]:
    nlat_o = lats.size // step
    nlon_o = lons.size // step
    la = lats[: nlat_o * step].reshape(nlat_o, step).mean(axis=1)
    lo = lons[: nlon_o * step].reshape(nlon_o, step).mean(axis=1)
    return la, lo


def _build_overview(base: TerrainLibrary, step: int) -> TerrainLibrary | None:
    """Block-mean overview at power-of-2 ``step``."""
    if step <= 1:
        return base
    nlat_o = base.lats.size // step
    nlon_o = base.lons.size // step
    if nlat_o < 2 or nlon_o < 2:
        return None
    la, lo = _downsample_axes(base.lats, base.lons, step)
    ku = kv = None
    if base.katabatic_u is not None and base.katabatic_v is not None:
        ku = _block_mean_spatial(base.katabatic_u, step)
        kv = _block_mean_spatial(base.katabatic_v, step)
    return TerrainLibrary(
        lats=la,
        lons=lo,
        speedup=_block_mean_spatial(base.speedup, step),
        deflection_deg=_block_mean_deflection(base.deflection_deg, step),
        katabatic_u=ku,
        katabatic_v=kv,
        version=base.version,
        source=base.source,
    )


def _load_full_library(path: Path) -> _FullLibraryPack | None:
    """Decode an entire Zarr library once; apply sea-mask + overview pyramids."""
    hit = _full_cache_get(path)
    if hit is not None:
        return hit

    import xarray as xr  # noqa: PLC0415

    t0 = time.perf_counter()
    src = "unknown"
    ds = xr.open_zarr(path)
    try:
        lats = np.asarray(ds["lat"].values, dtype=float)
        lons = np.asarray(ds["lon"].values, dtype=float)
        if lats.size < 2 or lons.size < 2:
            return None
        # Decode full cubes once; windows are numpy views / overview slices.
        su = np.asarray(ds["speedup"].values, dtype=np.float32).copy()
        de = np.asarray(ds["deflection_deg"].values, dtype=np.float32).copy()
        ku = kv = None
        if "katabatic_u" in ds and "katabatic_v" in ds:
            ku = np.asarray(ds["katabatic_u"].values, dtype=np.float32).copy()
            kv = np.asarray(ds["katabatic_v"].values, dtype=np.float32).copy()
        src = str(ds.attrs.get("source", "unknown"))
    finally:
        ds.close()

    land = _load_or_build_land_mask(path, lats, lons)
    if land is not None:
        _apply_sea_only_mask(su, de, ku, kv, land)
    _apply_identity_deadband_and_clamp(su, de)

    base = TerrainLibrary(
        lats=lats,
        lons=lons,
        speedup=su,
        deflection_deg=de,
        katabatic_u=ku,
        katabatic_v=kv,
        version=LIBRARY_VERSION,
        source=src,
    )

    overviews: dict[int, TerrainLibrary] = {}
    t1 = time.perf_counter()
    for step in _OVERVIEW_LEVELS:
        ov = _build_overview(base, step)
        if ov is not None and ov.available:
            overviews[step] = ov
    logger.info(
        "terrain library full-load %s shape=%s overviews=%s sea_mask=%s "
        "decode+mask=%.0fms pyramids=%.0fms",
        path.name,
        base.speedup.shape,
        sorted(overviews),
        land is not None,
        (t1 - t0) * 1000,
        (time.perf_counter() - t1) * 1000,
    )
    pack = _FullLibraryPack(base=base, overviews=overviews)
    _full_cache_put(path, pack)
    return pack


def _pick_overview_level(pack: _FullLibraryPack, overview: int) -> TerrainLibrary:
    """Select base or nearest available overview level ≤ requested step."""
    step = max(1, int(overview))
    if step <= 1:
        return pack.base
    if step in pack.overviews:
        return pack.overviews[step]
    # Nearest available power-of-2 at or below step.
    candidates = [s for s in pack.overviews if s <= step]
    if not candidates:
        return pack.base
    return pack.overviews[max(candidates)]


def _window_library(
    pack: _FullLibraryPack, bbox: BBox, *, overview: int = 1
) -> TerrainLibrary | None:
    """Window of the matching overview covering bbox, or None if out of bounds."""
    full = _pick_overview_level(pack, overview)
    if not _bbox_intersects_library(bbox, full):
        return None

    lats = full.lats
    lons = full.lons
    i0 = max(0, int(np.searchsorted(lats, bbox.lat_min) - 1))
    i1 = min(lats.size, int(np.searchsorted(lats, bbox.lat_max)) + 1)
    j0 = max(0, int(np.searchsorted(lons, bbox.lon_min) - 1))
    j1 = min(lons.size, int(np.searchsorted(lons, bbox.lon_max)) + 1)

    # Require a real overlap of at least 2 cells after clamping — do not invent
    # a 2×2 edge window for a bbox that only grazed the searchsorted clamp.
    if i1 - i0 < 2 or j1 - j0 < 2:
        return None

    la = lats[i0:i1]
    lo = lons[j0:j1]
    if la.size < 2 or lo.size < 2:
        return None

    ku = kv = None
    if full.katabatic_u is not None and full.katabatic_v is not None:
        ku = full.katabatic_u[:, :, i0:i1, j0:j1]
        kv = full.katabatic_v[:, :, i0:i1, j0:j1]

    return TerrainLibrary(
        lats=la,
        lons=lo,
        speedup=full.speedup[:, :, i0:i1, j0:j1],
        deflection_deg=full.deflection_deg[:, :, i0:i1, j0:j1],
        katabatic_u=ku,
        katabatic_v=kv,
        version=full.version,
        source=full.source,
    )


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
