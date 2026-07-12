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
- Axes (times, lats, lons) are always ascending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .schemas import BBox

MS_TO_KN = 1.943844


def _to_epoch(t: datetime) -> float:
    return t.timestamp()


def _fill_nan_nearest(arr: np.ndarray) -> np.ndarray:
    """Replace NaNs with spatially nearest finite neighbour (2D Euclidean).

    Avoids row-major 1D fill, which painted horizontal streaks along latitude
    rows near land / out-of-domain holes after regrid.
    """
    from scipy.ndimage import distance_transform_edt  # noqa: PLC0415

    out = np.asarray(arr, dtype=float).copy()
    if out.ndim != 2:
        raise ValueError(f"_fill_nan_nearest expects 2D array, got shape {out.shape}")
    if np.isfinite(out).all():
        return out
    nans = ~np.isfinite(out)
    if not nans.any():
        return out
    if nans.all():
        out[:] = 0.0
        return out
    # For each NaN, copy the value at the nearest finite pixel.
    indices = distance_transform_edt(nans, return_distances=False, return_indices=True)
    return out[tuple(indices)]


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

    @staticmethod
    def build(
        model_id: str,
        times_epoch: np.ndarray | list[float],
        lats: np.ndarray | list[float],
        lons: np.ndarray | list[float],
        u10: np.ndarray,
        v10: np.ndarray,
    ) -> WindField:
        """Build a WindField with ascending axes and validated shapes."""
        times = np.asarray(times_epoch, dtype=float).reshape(-1)
        la = np.asarray(lats, dtype=float).reshape(-1)
        lo = np.asarray(lons, dtype=float).reshape(-1)
        # float32 matches on-disk Zarr and halves RAM vs float64 grids.
        u = np.asarray(u10, dtype=np.float32)
        v = np.asarray(v10, dtype=np.float32)

        # Sort time ascending.
        t_order = np.argsort(times)
        times = times[t_order]
        u = u[t_order]
        v = v[t_order]

        # Sort lat ascending (flip arrays if north-first).
        if la.size >= 2 and la[0] > la[-1]:
            la = la[::-1].copy()
            u = u[:, ::-1, :]
            v = v[:, ::-1, :]
        elif la.size >= 2:
            lat_order = np.argsort(la)
            if not np.array_equal(lat_order, np.arange(la.size)):
                la = la[lat_order]
                u = u[:, lat_order, :]
                v = v[:, lat_order, :]

        # Sort lon ascending.
        if lo.size >= 2 and lo[0] > lo[-1]:
            lo = lo[::-1].copy()
            u = u[:, :, ::-1]
            v = v[:, :, ::-1]
        elif lo.size >= 2:
            lon_order = np.argsort(lo)
            if not np.array_equal(lon_order, np.arange(lo.size)):
                lo = lo[lon_order]
                u = u[:, :, lon_order]
                v = v[:, :, lon_order]

        field = WindField(model_id=model_id, times=times, lats=la, lons=lo, u10=u, v10=v)
        field.validate()
        return field

    def validate(self) -> None:
        """Assert monotonic ascending axes, finite values, and matching shapes."""
        nt, nlat, nlon = int(self.times.size), int(self.lats.size), int(self.lons.size)
        if self.u10.shape != (nt, nlat, nlon) or self.v10.shape != (nt, nlat, nlon):
            raise ValueError(
                f"u/v shape mismatch: u={self.u10.shape} v={self.v10.shape} "
                f"expected ({nt},{nlat},{nlon})"
            )
        if nt == 0 or nlat == 0 or nlon == 0:
            raise ValueError("WindField axes must be non-empty")
        if nt > 1 and not bool(np.all(np.diff(self.times) > 0)):
            raise ValueError("times must be strictly ascending")
        if nlat > 1 and not bool(np.all(np.diff(self.lats) > 0)):
            raise ValueError("lats must be strictly ascending")
        if nlon > 1 and not bool(np.all(np.diff(self.lons) > 0)):
            raise ValueError("lons must be strictly ascending")
        if not (np.isfinite(self.u10).all() and np.isfinite(self.v10).all()):
            raise ValueError("u10/v10 must be finite")

    @staticmethod
    def concat_time(fields: list[WindField]) -> WindField:
        """Stack per-step fields along time, dedupe + sort."""
        if not fields:
            raise ValueError("concat_time requires at least one field")
        model_id = fields[0].model_id
        lats = fields[0].lats
        lons = fields[0].lons
        times_list: list[float] = []
        u_list: list[np.ndarray] = []
        v_list: list[np.ndarray] = []
        seen: set[float] = set()
        for f in fields:
            if f.model_id != model_id:
                raise ValueError("concat_time: model_id mismatch")
            # Size check first — allclose on unequal lengths raises a broadcast
            # ValueError that looks like a GRIB parse failure upstream.
            if f.lats.size != lats.size or f.lons.size != lons.size:
                raise ValueError(
                    f"concat_time: spatial grid mismatch "
                    f"({f.lats.size}x{f.lons.size} vs {lats.size}x{lons.size})"
                )
            if not np.allclose(f.lats, lats) or not np.allclose(f.lons, lons):
                raise ValueError("concat_time: spatial grid mismatch")
            for i, te in enumerate(f.times):
                key = float(te)
                if key in seen:
                    continue
                seen.add(key)
                times_list.append(key)
                u_list.append(f.u10[i])
                v_list.append(f.v10[i])
        order = np.argsort(np.asarray(times_list))
        times = np.asarray(times_list, dtype=float)[order]
        u10 = np.stack([u_list[i] for i in order], axis=0)
        v10 = np.stack([v_list[i] for i in order], axis=0)
        return WindField.build(model_id, times, lats, lons, u10, v10)

    def subset_bbox(self, bbox: BBox) -> WindField:
        """Index-slice to bbox (inclusive pad by 1 cell)."""
        i0 = max(0, int(np.searchsorted(self.lats, bbox.lat_min) - 1))
        i1 = min(self.lats.size, int(np.searchsorted(self.lats, bbox.lat_max)) + 1)
        j0 = max(0, int(np.searchsorted(self.lons, bbox.lon_min) - 1))
        j1 = min(self.lons.size, int(np.searchsorted(self.lons, bbox.lon_max)) + 1)
        i1 = max(i1, i0 + 1)
        j1 = max(j1, j0 + 1)
        return WindField.build(
            self.model_id,
            self.times,
            self.lats[i0:i1],
            self.lons[j0:j1],
            self.u10[:, i0:i1, j0:j1],
            self.v10[:, i0:i1, j0:j1],
        )

    def regrid(self, lats_t: np.ndarray, lons_t: np.ndarray) -> WindField:
        """Bilinear u/v onto target axes for every time slice (space normalize)."""
        from scipy.interpolate import RegularGridInterpolator  # noqa: PLC0415

        lats_t = np.asarray(lats_t, dtype=float).reshape(-1)
        lons_t = np.asarray(lons_t, dtype=float).reshape(-1)
        qla = np.clip(lats_t, float(self.lats[0]), float(self.lats[-1]))
        qlo = np.clip(lons_t, float(self.lons[0]), float(self.lons[-1]))
        la_mesh, lo_mesh = np.meshgrid(qla, qlo, indexing="ij")
        pts = np.column_stack([la_mesh.ravel(), lo_mesh.ravel()])
        shape = (lats_t.size, lons_t.size)
        u_out = np.empty((self.n_times, *shape), dtype=np.float32)
        v_out = np.empty_like(u_out)
        for k in range(self.n_times):
            u_interp = RegularGridInterpolator(
                (self.lats, self.lons),
                self.u10[k],
                bounds_error=False,
                fill_value=np.nan,
            )
            v_interp = RegularGridInterpolator(
                (self.lats, self.lons),
                self.v10[k],
                bounds_error=False,
                fill_value=np.nan,
            )
            u_out[k] = _fill_nan_nearest(u_interp(pts).reshape(shape))
            v_out[k] = _fill_nan_nearest(v_interp(pts).reshape(shape))
        return WindField.build(self.model_id, self.times, lats_t, lons_t, u_out, v_out)

    def resample_hourly(self, start_epoch: float, end_epoch: float) -> WindField:
        """Linear-in-time interp onto whole-hour UTC axis (time normalize)."""
        t = self.times
        if t.size == 0:
            raise ValueError("resample_hourly: empty field")
        lo = max(math.ceil(float(t[0]) / 3600) * 3600, math.ceil(start_epoch / 3600) * 3600)
        hi = min(math.floor(float(t[-1]) / 3600) * 3600, math.floor(end_epoch / 3600) * 3600)
        if hi < lo:
            # Window shorter than one whole hour — pick nearest whole hour inside field.
            mid = 0.5 * (start_epoch + end_epoch)
            lo = round(mid / 3600) * 3600
            lo = float(np.clip(lo, t[0], t[-1]))
            hi = lo
        target = np.arange(lo, hi + 1, 3600, dtype=float)
        if target.size == 0:
            target = np.array([lo], dtype=float)

        if t.size == 1:
            u = np.repeat(self.u10, target.size, axis=0)
            v = np.repeat(self.v10, target.size, axis=0)
            return WindField.build(self.model_id, target, self.lats, self.lons, u, v)

        idx = np.clip(np.searchsorted(t, target, side="right") - 1, 0, t.size - 2)
        span = np.where(t[idx + 1] == t[idx], 1.0, t[idx + 1] - t[idx])
        w = ((target - t[idx]) / span)[:, None, None]
        u = self.u10[idx] * (1.0 - w) + self.u10[idx + 1] * w
        v = self.v10[idx] * (1.0 - w) + self.v10[idx + 1] * w
        return WindField.build(self.model_id, target, self.lats, self.lons, u, v)

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
        if self.times.size == 1:
            return self._bilinear(self.u10[0], lat, lon), self._bilinear(self.v10[0], lat, lon)
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

    def uv_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float] | None:
        """Return (u, v) m/s (east, north) or None if outside the time window."""
        return self._uv_at(lat, lon, t)

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

    # --- persistence (npz kept for tests/fixtures; production uses Zarr) ----

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
        return cls.build(
            model_id=str(z["model_id"]),
            times_epoch=z["times"],
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
        return WindField.build(
            model_id=model_id,
            times_epoch=np.array([_to_epoch(t) for t in times], dtype=float),
            lats=lats,
            lons=lons,
            u10=u,
            v10=v,
        )
