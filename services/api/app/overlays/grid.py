"""Compact regular-grid serialization for wind map overlays."""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from ..metocean import MS_TO_KN, WindField
from ..schemas import BBox


def wind_grid_payload(
    field: WindField,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    t: datetime,
    *,
    nlat: int,
    nlon: int,
) -> dict:
    """Return a compact u/v/speed grid for one time slice at explicit dims."""
    bbox = BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    lats = np.linspace(lat_min, lat_max, nlat)
    lons = np.linspace(lon_min, lon_max, nlon)

    u_flat: list[float] = []
    v_flat: list[float] = []
    speed_flat: list[float] = []
    for la in lats:
        for lo in lons:
            uv = field.uv_at(float(la), float(lo), t)
            if uv is None:
                u_flat.append(0.0)
                v_flat.append(0.0)
                speed_flat.append(0.0)
            else:
                u, v = uv
                u_flat.append(round(u, 4))
                v_flat.append(round(v, 4))
                speed_flat.append(round(math.hypot(u, v) * MS_TO_KN, 2))

    dlat = float((lat_max - lat_min) / max(1, nlat - 1))
    dlon = float((lon_max - lon_min) / max(1, nlon - 1))
    return {
        "lat0": float(lats[0]),
        "lon0": float(lons[0]),
        "dlat": dlat,
        "dlon": dlon,
        "nlat": nlat,
        "nlon": nlon,
        "u": u_flat,
        "v": v_flat,
        "speed_kn": speed_flat,
        "t_utc": t.isoformat(),
        "model_id": field.model_id,
        "bbox": bbox.model_dump(),
    }
