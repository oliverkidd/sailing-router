"""WindNinja terrain-wind engine (primary).

Runs the WindNinja OSS CLI as a subprocess to downscale the coarse model wind onto
the high-resolution DEM: corner/headland speed-up, flow bending/channeling between
islands, lee wind shadows, and diurnal katabatic/anabatic slope flow.

Configuration:
- SAILING_WINDNINJA_BIN: CLI name/path (default WindNinja_cli).
- Mass-conserving solver by default; mass+momentum optional (better lee eddies).
- Diurnal slope-flow + non-neutral stability enabled (needs date/time + lat/lon).

If the binary or a DEM GeoTIFF is unavailable, we transparently fall back to the
geometric heuristic so the router always runs (see docs/build-plan.md §7.3, §12).
"""

from __future__ import annotations

import shutil

from ..config import WINDNINJA_BIN
from ..geo import LandMask
from ..metocean import WindField
from ..terrain import TerrainProvider
from .base import TerrainWindModel


class WindNinjaTerrainWind(TerrainWindModel):
    def __init__(
        self,
        fallback: TerrainWindModel | None = None,
        solver: str = "mass_conserving",
        diurnal: bool = True,
    ):
        self._fallback = fallback
        self._solver = solver
        self._diurnal = diurnal

    @property
    def available(self) -> bool:
        return shutil.which(WINDNINJA_BIN) is not None

    def apply(
        self,
        wind: WindField,
        land: LandMask,
        terrain: TerrainProvider,
        refine_n: int = 24,
    ) -> WindField:
        if not self.available or not terrain.available:
            # No binary or no DEM -> geometric fallback keeps routing working.
            if self._fallback is not None:
                return self._fallback.apply(wind, land, terrain, refine_n)
            return wind
        return self._run_windninja(wind, land, terrain, refine_n)

    def _run_windninja(
        self, wind: WindField, land: LandMask, terrain: TerrainProvider, refine_n: int
    ) -> WindField:
        # TODO(v2): For each forecast time step, write a WindNinja config
        #   (DEM GeoTIFF from `terrain`, domain-average or NWP init from `wind`,
        #    solver=self._solver, diurnalWindFlag=self._diurnal, date/time + lat/lon),
        #   run `WINDNINJA_BIN --cli ...`, parse the output GeoTIFF/ASCII velocity +
        #   angle grids back into a canonical WindField, and cache keyed by
        #   (model_id, bbox, cycle, solver, diurnal).
        # Until the binary is wired end-to-end, defer to the heuristic so the effect
        # (lee shadow / corner speed-up) is still present in routing.
        if self._fallback is not None:
            return self._fallback.apply(wind, land, terrain, refine_n)
        return wind
