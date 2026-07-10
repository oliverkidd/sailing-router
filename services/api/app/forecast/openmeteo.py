"""Open-Meteo forecast connector (default).

One free HTTP JSON endpoint serves all European models — no key, no US infra. We
request wind_speed_10m + wind_direction_10m (m/s, meteorological direction) on a
small lat/lon grid over the bbox and assemble a canonical WindField.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import numpy as np

from ..config import OPEN_METEO_MODEL_MAP
from ..metocean import WindField
from ..schemas import BBox
from .base import ForecastProvider

_URL = "https://api.open-meteo.com/v1/forecast"
_GRID_N = 6  # points per axis over the bbox (small; terrain wind refines near coasts)


def _linspace_axis(lo: float, hi: float, n: int) -> np.ndarray:
    if hi <= lo:
        return np.array([lo], dtype=float)
    return np.linspace(lo, hi, n, dtype=float)


class OpenMeteoProvider(ForecastProvider):
    def __init__(self, client: httpx.Client | None = None, grid_n: int = _GRID_N):
        self._client = client
        self._grid_n = grid_n

    def _get_client(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=60.0)

    def fetch(
        self,
        model_id: str,
        bbox: BBox,
        cycle: str | None,
        horizon_h: int,
    ) -> WindField:
        om_model = OPEN_METEO_MODEL_MAP.get(model_id)
        if om_model is None:
            raise ValueError(f"No Open-Meteo mapping for model '{model_id}' (European only)")

        lats = _linspace_axis(bbox.lat_min, bbox.lat_max, self._grid_n)
        lons = _linspace_axis(bbox.lon_min, bbox.lon_max, self._grid_n)

        # Row-major grid of points, one request for all coordinates.
        lat_list, lon_list = [], []
        for la in lats:
            for lo in lons:
                lat_list.append(round(float(la), 4))
                lon_list.append(round(float(lo), 4))

        params = {
            "latitude": ",".join(str(x) for x in lat_list),
            "longitude": ",".join(str(x) for x in lon_list),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "ms",
            "models": om_model,
            "forecast_hours": max(1, horizon_h),
            "timezone": "UTC",
        }

        client = self._get_client()
        close = self._client is None
        try:
            resp = client.get(_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        finally:
            if close:
                client.close()

        return self._to_wind_field(model_id, data, lats, lons)

    @staticmethod
    def _to_wind_field(
        model_id: str,
        data: dict | list,
        lats: np.ndarray,
        lons: np.ndarray,
    ) -> WindField:
        # Open-Meteo returns a list for multiple coordinates, an object for one.
        points = data if isinstance(data, list) else [data]
        nlat, nlon = lats.size, lons.size
        if len(points) != nlat * nlon:
            raise ValueError(
                f"Open-Meteo returned {len(points)} points, expected {nlat * nlon}"
            )

        time_strs = points[0]["hourly"]["time"]
        times = [
            datetime.fromisoformat(t).replace(tzinfo=UTC) for t in time_strs
        ]
        nt = len(times)

        speed = np.zeros((nt, nlat, nlon), dtype=float)
        direction = np.zeros((nt, nlat, nlon), dtype=float)
        idx = 0
        for i in range(nlat):
            for j in range(nlon):
                hourly = points[idx]["hourly"]
                spd = np.array(hourly["wind_speed_10m"], dtype=float)
                drc = np.array(hourly["wind_direction_10m"], dtype=float)
                # Guard against nulls (Open-Meteo sends null past the horizon).
                spd = np.nan_to_num(spd, nan=0.0)
                drc = np.nan_to_num(drc, nan=0.0)
                speed[:, i, j] = spd[:nt]
                direction[:, i, j] = drc[:nt]
                idx += 1

        return WindField.from_speed_dir(model_id, times, lats, lons, speed, direction)
