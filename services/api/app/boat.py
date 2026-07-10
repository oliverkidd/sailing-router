"""Boat model: wraps a BoatConfig + PolarTable to drive the router.

Responsibilities:
- Effective boat speed = polar interpolation * polar_efficiency.
- Target beat/run angles: manual overrides if set, else derived from polar VMG.
- Leeway (wind drift): shifts the boat's water-track downwind of its heading.
"""

from __future__ import annotations

from dataclasses import dataclass

from .navmath import signed_diff
from .polar import PolarTable
from .schemas import BoatConfig

_EPS = 1e-3


@dataclass
class Boat:
    config: BoatConfig
    polar: PolarTable

    def bsp(self, tws: float, twa: float) -> float:
        """Boat speed through water (knots), derated by polar_efficiency."""
        return self.polar.speed_for(tws, twa) * self.config.polar_efficiency

    def target_upwind_twa(self, tws: float) -> float:
        if self.config.upwind_twa_deg is not None:
            return self.config.upwind_twa_deg
        return self.polar.best_vmg_angle(tws, upwind=True)[0]

    def target_downwind_twa(self, tws: float) -> float:
        if self.config.downwind_twa_deg is not None:
            return self.config.downwind_twa_deg
        return self.polar.best_vmg_angle(tws, upwind=False)[0]

    def leeway_deg(self, tws: float, bsp: float) -> float:
        """Leeway magnitude in degrees.

        leeway_deg = clamp(leeway_coeff * TWS / max(bsp, eps)^2, 0, leeway_max_deg).
        leeway_coeff = 0 disables it (default off).
        """
        c = self.config.leeway_coeff
        if c <= 0.0:
            return 0.0
        mag = c * tws / (max(bsp, _EPS) ** 2)
        return float(min(max(mag, 0.0), self.config.leeway_max_deg))

    def leeway_offset(self, tws: float, bsp: float, twd: float, heading: float) -> float:
        """Signed heading offset (deg) from leeway: the water-track is pushed to the
        downwind side of the heading. Returns 0 when leeway is disabled.
        """
        mag = self.leeway_deg(tws, bsp)
        if mag == 0.0:
            return 0.0
        # Downwind direction is where the wind is going (twd + 180). Push the track
        # toward that side of the heading.
        downwind_dir = (twd + 180.0) % 360.0
        side = signed_diff(downwind_dir, heading)
        sign = 1.0 if side >= 0 else -1.0
        return sign * mag
