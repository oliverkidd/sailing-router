"""Synthetic wind fields with the same ``wind_at(lat, lon, t)`` shape as GRIB.

No ``times`` attribute: commits are not clipped to a forecast epoch. Time-varying
fields change inside ``wind_at``, which is what the router already samples.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.navmath import norm360


def _elapsed_s(t: datetime, t0: datetime) -> float:
    return (t - t0).total_seconds()


@dataclass(frozen=True)
class ConstantWind:
    tws: float
    twd: float
    kind: str = "constant"


@dataclass(frozen=True)
class ShiftWind:
    """Piecewise-constant direction. ``twd0`` until ``shift_after_s``, then ``twd1``."""

    tws: float
    twd0: float
    twd1: float
    shift_after_s: float
    kind: str = "shift"


@dataclass(frozen=True)
class OscillationWind:
    """Square wave around ``twd_mean``. Period is short so chasing every flip is costly."""

    tws: float
    twd_mean: float
    amplitude_deg: float
    period_s: float
    kind: str = "oscillation"


@dataclass(frozen=True)
class DyingWind:
    """Spatially uniform breeze that fades from ``tws0`` to ``tws1`` over ``duration_s``."""

    tws0: float
    tws1: float
    twd: float
    duration_s: float
    kind: str = "dying"


@dataclass(frozen=True)
class GradientWind:
    """More wind to the east of ``lon0``. TWD is constant."""

    tws_lo: float
    tws_hi: float
    twd: float
    lon0: float
    span_nm: float
    kind: str = "gradient"


WindSpec = ConstantWind | ShiftWind | OscillationWind | DyingWind | GradientWind


class SynthWind:
    """Duck-typed wind field. ``t0`` is the race departure, not a later re-route."""

    def __init__(self, spec: WindSpec, t0: datetime) -> None:
        self.spec = spec
        self.t0 = t0
        self.time_varying = not isinstance(spec, ConstantWind | GradientWind)
        self.space_varying = isinstance(spec, GradientWind)
        self.tws_ceiling = _ceiling(spec)
        self.label = _label(spec)

    def wind_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float] | None:
        spec = self.spec
        elapsed = _elapsed_s(t, self.t0)
        if isinstance(spec, ConstantWind):
            return spec.tws, spec.twd
        if isinstance(spec, ShiftWind):
            twd = spec.twd0 if elapsed < spec.shift_after_s else spec.twd1
            return spec.tws, norm360(twd)
        if isinstance(spec, OscillationWind):
            if spec.period_s <= 0:
                return spec.tws, norm360(spec.twd_mean)
            phase = elapsed % spec.period_s
            sign = 1.0 if phase < spec.period_s / 2.0 else -1.0
            return spec.tws, norm360(spec.twd_mean + sign * spec.amplitude_deg)
        if isinstance(spec, DyingWind):
            if spec.duration_s <= 0:
                u = 1.0
            else:
                u = min(1.0, max(0.0, elapsed / spec.duration_s))
            return spec.tws0 + (spec.tws1 - spec.tws0) * u, norm360(spec.twd)
        east_nm = (lon - spec.lon0) * 60.0 * math.cos(math.radians(lat))
        frac = min(1.0, max(0.0, east_nm / spec.span_nm)) if spec.span_nm else 0.0
        return spec.tws_lo + (spec.tws_hi - spec.tws_lo) * frac, norm360(spec.twd)


def build_wind(spec: WindSpec, t0: datetime) -> SynthWind:
    return SynthWind(spec, t0)


def _ceiling(spec: WindSpec) -> float:
    if isinstance(spec, ConstantWind | ShiftWind | OscillationWind):
        return spec.tws
    if isinstance(spec, DyingWind):
        return max(spec.tws0, spec.tws1)
    return max(spec.tws_lo, spec.tws_hi)


def _label(spec: WindSpec) -> str:
    if isinstance(spec, ConstantWind):
        return f"constant {spec.tws:.0f}kn from {spec.twd:.0f}"
    if isinstance(spec, ShiftWind):
        return (
            f"shift {spec.tws:.0f}kn {spec.twd0:.0f}->{spec.twd1:.0f} "
            f"at {spec.shift_after_s:.0f}s"
        )
    if isinstance(spec, OscillationWind):
        return (
            f"oscillate ±{spec.amplitude_deg:.0f}° / {spec.period_s:.0f}s "
            f"around {spec.twd_mean:.0f}"
        )
    if isinstance(spec, DyingWind):
        return f"dying {spec.tws0:.0f}->{spec.tws1:.0f}kn from {spec.twd:.0f}"
    return f"gradient {spec.tws_lo:.0f}->{spec.tws_hi:.0f}kn from {spec.twd:.0f}"
