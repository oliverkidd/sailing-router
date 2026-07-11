"""Open-Meteo forecast connector (default).

Single free HTTP JSON endpoint for global models — no key required. We request
wind_speed_10m + wind_direction_10m on a lat/lon grid and assemble WindField.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx
import numpy as np

from ..config import OPEN_METEO_MODEL_MAP
from ..metocean import WindField
from ..schemas import BBox
from .base import ForecastProvider

_URL = "https://api.open-meteo.com/v1/forecast"
_DEFAULT_GRID_N = 6
_POST_THRESHOLD = 50
_MAX_RETRIES = 4
_RETRY_STATUSES = {429, 502, 503, 504}
logger = logging.getLogger(__name__)


def _linspace_axis(lo: float, hi: float, n: int) -> np.ndarray:
    if hi <= lo:
        return np.array([lo], dtype=float)
    return np.linspace(lo, hi, n, dtype=float)


class OpenMeteoProvider(ForecastProvider):
    def __init__(self, client: httpx.Client | None = None, grid_n: int = _DEFAULT_GRID_N):
        self._client = client
        self._grid_n = grid_n

    def _get_client(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=120.0)

    def fetch(
        self,
        model_id: str,
        bbox: BBox,
        cycle: str | None,
        horizon_h: int,
        *,
        grid_shape: tuple[int, int] | None = None,
    ) -> WindField:
        om_model = OPEN_METEO_MODEL_MAP.get(model_id)
        if om_model is None:
            raise ValueError(f"No Open-Meteo mapping for model '{model_id}'")

        if grid_shape is not None:
            nlat, nlon = grid_shape
        else:
            nlat = nlon = self._grid_n

        lats = _linspace_axis(bbox.lat_min, bbox.lat_max, nlat)
        lons = _linspace_axis(bbox.lon_min, bbox.lon_max, nlon)

        lat_list, lon_list = [], []
        for la in lats:
            for lo in lons:
                lat_list.append(round(float(la), 4))
                lon_list.append(round(float(lo), 4))

        days = max(1, min(16, -(-horizon_h // 24) + 1))
        params = {
            "latitude": ",".join(str(x) for x in lat_list),
            "longitude": ",".join(str(x) for x in lon_list),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "ms",
            "models": om_model,
            "forecast_days": days,
            "timezone": "UTC",
        }

        client = self._get_client()
        close = self._client is None
        try:
            data = self._request_with_retry(client, params, len(lat_list))
        finally:
            if close:
                client.close()

        return self._to_wind_field(model_id, data, lats, lons)

    def _request_with_retry(self, client: httpx.Client, params: dict, n_pts: int) -> dict | list:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                if n_pts >= _POST_THRESHOLD:
                    resp = client.post(_URL, data=params)
                else:
                    resp = client.get(_URL, params=params)
                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    wait = 1.5 * (2**attempt)
                    logger.warning(
                        "Open-Meteo %s, retry %d/%d in %.1fs",
                        resp.status_code,
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    reason = resp.text
                    try:
                        reason = resp.json().get("reason", reason)
                    except Exception:  # noqa: BLE001
                        pass
                    raise httpx.HTTPStatusError(
                        f"Open-Meteo {resp.status_code}: {reason}",
                        request=resp.request,
                        response=resp,
                    )
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    time.sleep(1.5 * (2**attempt))
                    continue
                raise
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(1.5 * (2**attempt))
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("Open-Meteo request failed")

    @staticmethod
    def _to_wind_field(
        model_id: str,
        data: dict | list,
        lats: np.ndarray,
        lons: np.ndarray,
    ) -> WindField:
        points = data if isinstance(data, list) else [data]
        nlat, nlon = lats.size, lons.size
        if len(points) != nlat * nlon:
            raise ValueError(
                f"Open-Meteo returned {len(points)} points, expected {nlat * nlon}"
            )

        time_strs = points[0]["hourly"]["time"]
        times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in time_strs]
        nt = len(times)

        speed = np.zeros((nt, nlat, nlon), dtype=float)
        direction = np.zeros((nt, nlat, nlon), dtype=float)
        idx = 0
        for i in range(nlat):
            for j in range(nlon):
                hourly = points[idx]["hourly"]
                spd = np.array(hourly["wind_speed_10m"], dtype=float)
                drc = np.array(hourly["wind_direction_10m"], dtype=float)
                spd = np.nan_to_num(spd, nan=0.0)
                drc = np.nan_to_num(drc, nan=0.0)
                speed[:, i, j] = spd[:nt]
                direction[:, i, j] = drc[:nt]
                idx += 1

        return WindField.from_speed_dir(model_id, times, lats, lons, speed, direction)
