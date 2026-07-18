"""WindField-compatible wrapper that applies terrain downscaling in wind_at/uv_at.

The hardened isochrone router keeps calling ``.wind_at(lat, lon, t)`` unchanged;
this wrapper makes those samples terrain-adjusted when a library is loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..metocean import WindField
from . import operator as op
from .terrain_library import TerrainLibrary

# Snap sample time to this bucket (matches default router time_step_s).
_TIME_BUCKET_S = 300
# ~1.1 km spatial cell for memoization during isochrone expansion.
_CACHE_CELL_DEG = 0.01


@dataclass
class DownscaledWindField:
    """Delegates grid storage to ``forcing``; samples via the downscale operator."""

    forcing: WindField
    library: TerrainLibrary | None = None
    operator_version: str = op.OPERATOR_VERSION
    _uv_cache: dict[tuple[int, int, int], tuple[float, float] | None] = field(
        default_factory=dict, repr=False, compare=False
    )

    # --- WindField surface used by the router / prepare --------------------

    @property
    def model_id(self) -> str:
        return self.forcing.model_id

    @property
    def times(self):
        return self.forcing.times

    @property
    def lats(self):
        return self.forcing.lats

    @property
    def lons(self):
        return self.forcing.lons

    @property
    def u10(self):
        return self.forcing.u10

    @property
    def v10(self):
        return self.forcing.v10

    @property
    def n_times(self) -> int:
        return self.forcing.n_times

    def _cache_key(self, lat: float, lon: float, t: datetime) -> tuple[int, int, int]:
        te = int(t.timestamp()) // _TIME_BUCKET_S
        return (
            int(round(lat / _CACHE_CELL_DEG)),
            int(round(lon / _CACHE_CELL_DEG)),
            te,
        )

    def uv_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float] | None:
        key = self._cache_key(lat, lon, t)
        if key in self._uv_cache:
            return self._uv_cache[key]
        uv = op.downscale_point(self.forcing, self.library, lat, lon, t)
        self._uv_cache[key] = uv
        return uv

    def wind_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float] | None:
        uv = self.uv_at(lat, lon, t)
        if uv is None:
            return None
        return op.wind_at_knots(*uv)

    def regrid(self, lats, lons) -> WindField:
        return op.downscale_grid(self.forcing, self.library, lats, lons)

    def subset_bbox(self, bbox):
        return DownscaledWindField(
            forcing=self.forcing.subset_bbox(bbox),
            library=self.library,
            operator_version=self.operator_version,
        )

    def resample_hourly(self, start_epoch: float, end_epoch: float) -> DownscaledWindField:
        return DownscaledWindField(
            forcing=self.forcing.resample_hourly(start_epoch, end_epoch),
            library=self.library,
            operator_version=self.operator_version,
        )
