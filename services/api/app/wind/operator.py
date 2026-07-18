"""Downscaling operator: forcing × terrain-response → surface wind.

v0 (library absent): identity — return forcing unchanged.
v1 (library present): apply speedup, deflection, and optional katabatic add.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from ..metocean import MS_TO_KN, WindField
from .terrain_library import TerrainLibrary, TerrainResponse, day_frac_utc

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
    la_mesh, lo_mesh = np.meshgrid(lats, lons, indexing="ij")
    u_out = np.empty((nt, nlat, nlon), dtype=np.float32)
    v_out = np.empty_like(u_out)

    for k in range(nt):
        te = float(base.times[k])
        u = base.u10[k]
        v = base.v10[k]
        dir_from = (np.degrees(np.arctan2(-u, -v))) % 360.0
        # Sample library at each cell (still pointwise for sector blend).
        su = np.empty((nlat, nlon), dtype=np.float32)
        de = np.empty((nlat, nlon), dtype=np.float32)
        ku = np.zeros((nlat, nlon), dtype=np.float32)
        kv = np.zeros((nlat, nlon), dtype=np.float32)
        for i in range(nlat):
            for j in range(nlon):
                resp = library.sample(
                    float(la_mesh[i, j]),
                    float(lo_mesh[i, j]),
                    wind_dir_from_deg=float(dir_from[i, j]),
                    day_frac=day_frac_utc(te, float(lo_mesh[i, j])),
                )
                su[i, j] = resp.speedup
                de[i, j] = resp.deflection_deg
                ku[i, j] = resp.katabatic_u
                kv[i, j] = resp.katabatic_v
        u_out[k], v_out[k] = apply_response_arrays(u, v, su, de, ku, kv)

    return WindField.build(base.model_id, base.times, lats, lons, u_out, v_out)


def wind_at_knots(u_ms: float, v_ms: float) -> tuple[float, float]:
    """(tws_kn, twd_from_deg) from u/v m/s."""
    tws = math.hypot(u_ms, v_ms) * MS_TO_KN
    twd = (math.degrees(math.atan2(-u_ms, -v_ms))) % 360.0
    return tws, twd
