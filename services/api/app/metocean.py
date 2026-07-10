"""Canonical metocean fields + samplers.

WindField is the single wind representation used everywhere downstream. It stores
u10/v10 (m/s) on a regular (time, lat, lon) grid and answers `wind_at(lat, lon, t)`
with bilinear-in-space + linear-in-time interpolation.

Conventions (pinned, see tests):
- u10 = eastward wind component, v10 = northward (m/s). The wind vector points in
  the direction the wind is blowing TOWARD.
- TWD returned by wind_at is the direction the wind blows FROM (meteorological),
  degrees clockwise from North.
- TWS returned in KNOTS.
- wind_at returns None when t is outside the forecast horizon (forecast exhausted).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np

MS_TO_KN = 1.943844


def _to_epoch(t: datetime) -> float:
    return t.timestamp()


@dataclass
class WindField:
    model_id: str
    times: np.ndarray  # (nt,) float epoch seconds, ascending
    lats: np.ndarray  # (nlat,) ascending
    lons: np.ndarray  # (nlon,) ascending
    u10: np.ndarray  # (nt, nlat, nlon) m/s
    v10: np.ndarray  # (nt, nlat, nlon) m/s

    @property
    def n_times(self) -> int:
        return int(self.times.size)

    def _bilinear(self, arr2d: np.ndarray, lat: float, lon: float) -> float:
        la = float(np.clip(lat, self.lats[0], self.lats[-1]))
        lo = float(np.clip(lon, self.lons[0], self.lons[-1]))
        i = int(np.clip(np.searchsorted(self.lats, la) - 1, 0, self.lats.size - 2))
        j = int(np.clip(np.searchsorted(self.lons, lo) - 1, 0, self.lons.size - 2))
        la0, la1 = self.lats[i], self.lats[i + 1]
        lo0, lo1 = self.lons[j], self.lons[j + 1]
        fy = 0.0 if la1 == la0 else (la - la0) / (la1 - la0)
        fx = 0.0 if lo1 == lo0 else (lo - lo0) / (lo1 - lo0)
        v0 = arr2d[i, j] * (1 - fx) + arr2d[i, j + 1] * fx
        v1 = arr2d[i + 1, j] * (1 - fx) + arr2d[i + 1, j + 1] * fx
        return float(v0 * (1 - fy) + v1 * fy)

    def _uv_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float] | None:
        te = _to_epoch(t)
        if te < self.times[0] or te > self.times[-1]:
            return None
        k = int(np.clip(np.searchsorted(self.times, te) - 1, 0, self.times.size - 2))
        t0, t1 = self.times[k], self.times[k + 1]
        ft = 0.0 if t1 == t0 else (te - t0) / (t1 - t0)
        u = self._bilinear(self.u10[k], lat, lon) * (1 - ft) + self._bilinear(
            self.u10[k + 1], lat, lon
        ) * ft
        v = self._bilinear(self.v10[k], lat, lon) * (1 - ft) + self._bilinear(
            self.v10[k + 1], lat, lon
        ) * ft
        return u, v

    def wind_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float] | None:
        """Return (tws_knots, twd_deg_from) or None if forecast exhausted."""
        uv = self._uv_at(lat, lon, t)
        if uv is None:
            return None
        u, v = uv
        tws = math.hypot(u, v) * MS_TO_KN
        # Direction the wind blows FROM: opposite of the (u,v) vector.
        twd = (math.degrees(math.atan2(-u, -v))) % 360.0
        return tws, twd

    # --- persistence (npz; TODO(v2): NetCDF/zarr) ---------------------------

    def save_npz(self, path) -> None:
        np.savez_compressed(
            path,
            model_id=self.model_id,
            times=self.times,
            lats=self.lats,
            lons=self.lons,
            u10=self.u10,
            v10=self.v10,
        )

    @classmethod
    def load_npz(cls, path) -> WindField:
        z = np.load(path, allow_pickle=True)
        return cls(
            model_id=str(z["model_id"]),
            times=z["times"],
            lats=z["lats"],
            lons=z["lons"],
            u10=z["u10"],
            v10=z["v10"],
        )

    @staticmethod
    def from_speed_dir(
        model_id: str,
        times: list[datetime],
        lats: np.ndarray,
        lons: np.ndarray,
        speed_ms: np.ndarray,  # (nt, nlat, nlon)
        dir_from_deg: np.ndarray,  # (nt, nlat, nlon), direction wind blows FROM
    ) -> WindField:
        """Build from wind speed + meteorological direction (blows FROM)."""
        rad = np.radians(dir_from_deg)
        # Wind blows TOWARD (dir_from + 180): u = -speed*sin(from), v = -speed*cos(from).
        u = -speed_ms * np.sin(rad)
        v = -speed_ms * np.cos(rad)
        return WindField(
            model_id=model_id,
            times=np.array([_to_epoch(t) for t in times], dtype=float),
            lats=np.asarray(lats, dtype=float),
            lons=np.asarray(lons, dtype=float),
            u10=u.astype(float),
            v10=v.astype(float),
        )


@dataclass
class CurrentField:
    """Surface current on a regular (time, lat, lon) grid, vars u,v in m/s.

    Convention: (u, v) is the vector the current flows TOWARD (east, north). The
    router's `current_at` returns (u, v) m/s; `set_rate_at` returns the compass
    "set" (direction toward) and rate in knots. Outside the grid/window the current
    is treated as zero (calm), never None — currents only shift timing, they don't
    exhaust a route.
    """

    source: str
    times: np.ndarray
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray  # (nt, nlat, nlon) m/s toward-east
    v: np.ndarray  # (nt, nlat, nlon) m/s toward-north

    def _bilinear(self, arr2d: np.ndarray, lat: float, lon: float) -> float:
        la = float(np.clip(lat, self.lats[0], self.lats[-1]))
        lo = float(np.clip(lon, self.lons[0], self.lons[-1]))
        i = int(np.clip(np.searchsorted(self.lats, la) - 1, 0, self.lats.size - 2))
        j = int(np.clip(np.searchsorted(self.lons, lo) - 1, 0, self.lons.size - 2))
        la0, la1 = self.lats[i], self.lats[i + 1]
        lo0, lo1 = self.lons[j], self.lons[j + 1]
        fy = 0.0 if la1 == la0 else (la - la0) / (la1 - la0)
        fx = 0.0 if lo1 == lo0 else (lo - lo0) / (lo1 - lo0)
        v0 = arr2d[i, j] * (1 - fx) + arr2d[i, j + 1] * fx
        v1 = arr2d[i + 1, j] * (1 - fx) + arr2d[i + 1, j + 1] * fx
        return float(v0 * (1 - fy) + v1 * fy)

    def current_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float]:
        """Return (u, v) surface current m/s. Zero outside grid extent / time window."""
        if lat < self.lats[0] or lat > self.lats[-1] or lon < self.lons[0] or lon > self.lons[-1]:
            return 0.0, 0.0
        te = _to_epoch(t)
        if te < self.times[0] or te > self.times[-1]:
            return 0.0, 0.0
        k = int(np.clip(np.searchsorted(self.times, te) - 1, 0, self.times.size - 2))
        t0, t1 = self.times[k], self.times[k + 1]
        ft = 0.0 if t1 == t0 else (te - t0) / (t1 - t0)
        u = self._bilinear(self.u[k], lat, lon) * (1 - ft) + self._bilinear(self.u[k + 1], lat, lon) * ft
        v = self._bilinear(self.v[k], lat, lon) * (1 - ft) + self._bilinear(self.v[k + 1], lat, lon) * ft
        return u, v

    def save_npz(self, path) -> None:
        np.savez_compressed(
            path, source=self.source, times=self.times, lats=self.lats, lons=self.lons, u=self.u, v=self.v
        )

    @classmethod
    def load_npz(cls, path) -> CurrentField:
        z = np.load(path, allow_pickle=True)
        return cls(
            source=str(z["source"]),
            times=z["times"],
            lats=z["lats"],
            lons=z["lons"],
            u=z["u"],
            v=z["v"],
        )
