"""Downscaling operator: forcing × terrain-response → surface wind.

v0 (library absent): identity — return forcing unchanged.
v1 (library present): apply speedup, deflection, and optional katabatic add.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from ..metocean import MS_TO_KN, WindField
from .terrain_library import (
    N_SECTORS,
    SECTOR_DEG,
    TerrainLibrary,
    TerrainResponse,
    day_frac_utc,
)

# Below this background speed (m/s), blend in the katabatic add strongly.
_KATABATIC_BLEND_MS = 2.0
OPERATOR_VERSION = "v1"


def apply_response(
    u_ms: float,
    v_ms: float,
    resp: TerrainResponse,
) -> tuple[float, float]:
    """Apply terrain response to a single (u,v) sample in m/s."""
    speed = math.hypot(u_ms, v_ms)
    if speed < 1e-6:
        return resp.katabatic_u, resp.katabatic_v

    dir_from = (math.degrees(math.atan2(-u_ms, -v_ms))) % 360.0
    dir_hi = (dir_from + resp.deflection_deg) % 360.0
    speed_hi = speed * max(0.05, float(resp.speedup))

    toward = (dir_hi + 180.0) % 360.0
    rad = math.radians(toward)
    u = speed_hi * math.sin(rad)
    v = speed_hi * math.cos(rad)

    if abs(resp.katabatic_u) > 1e-6 or abs(resp.katabatic_v) > 1e-6:
        w = float(np.clip(1.0 - speed / _KATABATIC_BLEND_MS, 0.0, 1.0))
        u = u * (1 - w) + (u + resp.katabatic_u) * w
        v = v * (1 - w) + (v + resp.katabatic_v) * w
    return float(u), float(v)


def apply_response_arrays(
    u: np.ndarray,
    v: np.ndarray,
    speedup: np.ndarray,
    deflection_deg: np.ndarray,
    katabatic_u: np.ndarray | None = None,
    katabatic_v: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised apply_response over matching arrays."""
    speed = np.hypot(u, v)
    calm = speed < 1e-6
    dir_from = (np.degrees(np.arctan2(-u, -v))) % 360.0
    dir_hi = (dir_from + deflection_deg) % 360.0
    speed_hi = speed * np.maximum(0.05, speedup)
    toward = (dir_hi + 180.0) % 360.0
    rad = np.radians(toward)
    u_out = speed_hi * np.sin(rad)
    v_out = speed_hi * np.cos(rad)
    if katabatic_u is not None and katabatic_v is not None:
        w = np.clip(1.0 - speed / _KATABATIC_BLEND_MS, 0.0, 1.0)
        u_out = u_out * (1 - w) + (u_out + katabatic_u) * w
        v_out = v_out * (1 - w) + (v_out + katabatic_v) * w
        u_out = np.where(calm, katabatic_u, u_out)
        v_out = np.where(calm, katabatic_v, v_out)
    else:
        u_out = np.where(calm, 0.0, u_out)
        v_out = np.where(calm, 0.0, v_out)
    return u_out.astype(np.float32), v_out.astype(np.float32)


def downscale_point(
    forcing: WindField,
    library: TerrainLibrary | None,
    lat: float,
    lon: float,
    t: datetime,
) -> tuple[float, float] | None:
    """Return downscaled (u,v) m/s at a point, or None if forcing exhausted."""
    uv = forcing.uv_at(lat, lon, t)
    if uv is None:
        return None
    u, v = uv
    if library is None or not library.available:
        return u, v
    speed = math.hypot(u, v)
    dir_from = (
        (math.degrees(math.atan2(-u, -v))) % 360.0 if speed > 1e-6 else 0.0
    )
    te = t.timestamp()
    resp = library.sample(
        lat,
        lon,
        wind_dir_from_deg=dir_from,
        day_frac=day_frac_utc(te, lon),
    )
    return apply_response(u, v, resp)


def _spatial_weights(
    lib_lats: np.ndarray, lib_lons: np.ndarray, lats: np.ndarray, lons: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Bilinear indices/weights for display mesh on library axes + in-bounds mask."""
    la_mesh, lo_mesh = np.meshgrid(lats, lons, indexing="ij")
    in_b = (
        (la_mesh >= float(lib_lats[0]))
        & (la_mesh <= float(lib_lats[-1]))
        & (lo_mesh >= float(lib_lons[0]))
        & (lo_mesh <= float(lib_lons[-1]))
    )
    la = np.clip(la_mesh, lib_lats[0], lib_lats[-1])
    lo = np.clip(lo_mesh, lib_lons[0], lib_lons[-1])
    i = np.clip(np.searchsorted(lib_lats, la) - 1, 0, lib_lats.size - 2)
    j = np.clip(np.searchsorted(lib_lons, lo) - 1, 0, lib_lons.size - 2)
    la0 = lib_lats[i]
    la1 = lib_lats[i + 1]
    lo0 = lib_lons[j]
    lo1 = lib_lons[j + 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        fy = np.where(la1 == la0, 0.0, (la - la0) / (la1 - la0))
        fx = np.where(lo1 == lo0, 0.0, (lo - lo0) / (lo1 - lo0))
    return (
        i.astype(np.intp),
        j.astype(np.intp),
        fx.astype(np.float64),
        fy.astype(np.float64),
        in_b,
    )


def _bilinear_gather(
    arr: np.ndarray,
    i: np.ndarray,
    j: np.ndarray,
    fx: np.ndarray,
    fy: np.ndarray,
) -> np.ndarray:
    """Bilinear sample of 2d ``arr`` at vectorised (i,j,fx,fy)."""
    v0 = arr[i, j] * (1.0 - fx) + arr[i, j + 1] * fx
    v1 = arr[i + 1, j] * (1.0 - fx) + arr[i + 1, j + 1] * fx
    return v0 * (1.0 - fy) + v1 * fy


def _blend_sector_cube(
    cube: np.ndarray,
    i: np.ndarray,
    j: np.ndarray,
    fx: np.ndarray,
    fy: np.ndarray,
    s0: np.ndarray,
    s1: np.ndarray,
    ws: np.ndarray,
    wd: np.ndarray,
    wn: np.ndarray,
) -> np.ndarray:
    """Sector + day/night blend of library cube onto the display mesh."""
    nlat, nlon = i.shape
    day_stack = np.empty((N_SECTORS, nlat, nlon), dtype=np.float64)
    night_stack = np.empty_like(day_stack)
    for s in range(N_SECTORS):
        day_stack[s] = _bilinear_gather(cube[s, 0], i, j, fx, fy)
        night_stack[s] = _bilinear_gather(cube[s, 1], i, j, fx, fy)
    c00 = np.take_along_axis(day_stack, s0[np.newaxis, ...], axis=0)[0]
    c10 = np.take_along_axis(day_stack, s1[np.newaxis, ...], axis=0)[0]
    c01 = np.take_along_axis(night_stack, s0[np.newaxis, ...], axis=0)[0]
    c11 = np.take_along_axis(night_stack, s1[np.newaxis, ...], axis=0)[0]
    day = ws * c00 + (1.0 - ws) * c10
    night = ws * c01 + (1.0 - ws) * c11
    return wd * day + wn * night


def _sample_library_grid(
    library: TerrainLibrary,
    i: np.ndarray,
    j: np.ndarray,
    fx: np.ndarray,
    fy: np.ndarray,
    in_b: np.ndarray,
    dir_from: np.ndarray,
    day_frac: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised TerrainLibrary.sample over a full grid."""
    idx = (dir_from % 360.0) / SECTOR_DEG
    s0 = np.floor(idx).astype(np.intp) % N_SECTORS
    s1 = (s0 + 1) % N_SECTORS
    frac = idx - np.floor(idx)
    ws = 1.0 - frac
    wd = np.clip(day_frac, 0.0, 1.0)
    wn = 1.0 - wd

    su = np.where(
        in_b,
        _blend_sector_cube(library.speedup, i, j, fx, fy, s0, s1, ws, wd, wn),
        1.0,
    ).astype(np.float32)
    de = np.where(
        in_b,
        _blend_sector_cube(library.deflection_deg, i, j, fx, fy, s0, s1, ws, wd, wn),
        0.0,
    ).astype(np.float32)
    if library.katabatic_u is not None and library.katabatic_v is not None:
        ku = np.where(
            in_b,
            _blend_sector_cube(library.katabatic_u, i, j, fx, fy, s0, s1, ws, wd, wn),
            0.0,
        ).astype(np.float32)
        kv = np.where(
            in_b,
            _blend_sector_cube(library.katabatic_v, i, j, fx, fy, s0, s1, ws, wd, wn),
            0.0,
        ).astype(np.float32)
    else:
        ku = np.zeros_like(su)
        kv = np.zeros_like(su)
    return su, de, ku, kv


def downscale_grid(
    forcing: WindField,
    library: TerrainLibrary | None,
    lats: np.ndarray,
    lons: np.ndarray,
) -> WindField:
    """Materialise a downscaled WindField on (lats, lons) for all forcing times."""
    base = forcing.regrid(lats, lons)
    if library is None or not library.available:
        return base

    nt, nlat, nlon = base.n_times, lats.size, lons.size
    i, j, fx, fy, in_b = _spatial_weights(library.lats, library.lons, lats, lons)
    lon_row = np.asarray(lons, dtype=float)

    u_out = np.empty((nt, nlat, nlon), dtype=np.float32)
    v_out = np.empty_like(u_out)

    for k in range(nt):
        te = float(base.times[k])
        u = base.u10[k]
        v = base.v10[k]
        dir_from = (np.degrees(np.arctan2(-u, -v))) % 360.0
        hours = (te / 3600.0 + lon_row / 15.0) % 24.0
        day_frac_1d = 0.5 * (1.0 + np.cos(np.radians((hours - 12.0) * 15.0)))
        day_frac = np.broadcast_to(day_frac_1d, (nlat, nlon))
        su, de, ku, kv = _sample_library_grid(
            library, i, j, fx, fy, in_b, dir_from, day_frac
        )
        u_out[k], v_out[k] = apply_response_arrays(u, v, su, de, ku, kv)

    return WindField.build(base.model_id, base.times, lats, lons, u_out, v_out)


def wind_at_knots(u_ms: float, v_ms: float) -> tuple[float, float]:
    """(tws_kn, twd_from_deg) from u/v m/s."""
    tws = math.hypot(u_ms, v_ms) * MS_TO_KN
    twd = (math.degrees(math.atan2(-u_ms, -v_ms))) % 360.0
    return tws, twd
