"""Compact regular-grid serialization for wind map overlays."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from ..metocean import MS_TO_KN, WindField
from ..schemas import BBox


def display_series_payload(
    field: WindField,
    bbox: BBox,
    nlat: int,
    nlon: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Interpolate field onto an nlat×nlon target grid for times in [start, end].

    Emits the WindSeries JSON shape used by the frontend.
    When ``field`` is already on the target axes, skips RegularGridInterpolator.
    """
    lats = np.linspace(bbox.lat_min, bbox.lat_max, nlat)
    lons = np.linspace(bbox.lon_min, bbox.lon_max, nlon)
    axes_match = (
        field.lats.size == nlat
        and field.lons.size == nlon
        and np.allclose(field.lats, lats, rtol=0.0, atol=1e-9)
        and np.allclose(field.lons, lons, rtol=0.0, atol=1e-9)
    )

    start_epoch = start.timestamp() if start is not None else float(field.times[0])
    end_epoch = end.timestamp() if end is not None else float(field.times[-1])

    times_iso: list[str] = []
    u_series: list[list[float]] = []
    v_series: list[list[float]] = []
    speed_series: list[list[float]] = []

    if not axes_match:
        la_lo, la_hi = float(field.lats[0]), float(field.lats[-1])
        lo_lo, lo_hi = float(field.lons[0]), float(field.lons[-1])
        query_lat = np.clip(lats, la_lo, la_hi)
        query_lon = np.clip(lons, lo_lo, lo_hi)
        qq_lat, qq_lon = np.meshgrid(query_lat, query_lon, indexing="ij")
        pts = np.column_stack([qq_lat.ravel(), qq_lon.ravel()])

    for te in field.times:
        te_f = float(te)
        if te_f < start_epoch - 1 or te_f > end_epoch + 1:
            continue
        k = int(np.searchsorted(field.times, te_f))
        k = min(max(k, 0), field.times.size - 1)
        if abs(float(field.times[k]) - te_f) > 1 and k > 0:
            if abs(float(field.times[k - 1]) - te_f) < abs(float(field.times[k]) - te_f):
                k = k - 1

        t = datetime.fromtimestamp(te_f, tz=UTC)
        times_iso.append(t.isoformat().replace("+00:00", "Z"))

        if axes_match:
            u_flat = field.u10[k].astype(float)
            v_flat = field.v10[k].astype(float)
        else:
            u_interp = RegularGridInterpolator(
                (field.lats, field.lons),
                field.u10[k],
                bounds_error=False,
                fill_value=0.0,
            )
            v_interp = RegularGridInterpolator(
                (field.lats, field.lons),
                field.v10[k],
                bounds_error=False,
                fill_value=0.0,
            )
            u_flat = u_interp(pts).reshape(nlat, nlon).astype(float)
            v_flat = v_interp(pts).reshape(nlat, nlon).astype(float)
        speed = np.hypot(u_flat, v_flat) * MS_TO_KN

        u_series.append([round(float(x), 4) for x in u_flat.ravel()])
        v_series.append([round(float(x), 4) for x in v_flat.ravel()])
        speed_series.append([round(float(x), 2) for x in speed.ravel()])

    dlat = float((bbox.lat_max - bbox.lat_min) / max(1, nlat - 1))
    dlon = float((bbox.lon_max - bbox.lon_min) / max(1, nlon - 1))
    return {
        "lat0": float(lats[0]),
        "lon0": float(lons[0]),
        "dlat": dlat,
        "dlon": dlon,
        "nlat": nlat,
        "nlon": nlon,
        "times": times_iso,
        "u": u_series,
        "v": v_series,
        "speed_kn": speed_series,
        "model_id": field.model_id,
        "bbox": bbox.model_dump(),
    }


# Back-compat alias used by older tests if any.
def wind_series_payload(
    field: WindField,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    *,
    nlat: int,
    nlon: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    bbox = BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    return display_series_payload(field, bbox, nlat, nlon, start, end)
