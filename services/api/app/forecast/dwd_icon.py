"""DWD Open Data GRIB sources — ICON-EU and ICON-D2 (regular lat-lon)."""

from __future__ import annotations

import logging
from datetime import datetime

from ..metocean import WindField
from ..schemas import BBox
from .grib_source import (
    GribSource,
    SourceSpec,
    assemble_windfield,
    bunzip2,
    grib_bytes_to_component,
    http_get,
    map_steps_parallel,
    register,
)

logger = logging.getLogger(__name__)

_BASE = "https://opendata.dwd.de/weather/nwp"

# Finest native cadence: hourly.
_ICON_EU_STEPS = tuple(range(0, 79, 1))
_ICON_D2_STEPS = tuple(range(0, 49, 1))


def _icon_eu_url(cycle: datetime, step: int, var: str) -> str:
    hh = f"{cycle.hour:02d}"
    stamp = cycle.strftime("%Y%m%d%H")
    sss = f"{step:03d}"
    token = "U_10M" if var.startswith("u") else "V_10M"
    return (
        f"{_BASE}/icon-eu/grib/{hh}/{var}/"
        f"icon-eu_europe_regular-lat-lon_single-level_{stamp}_{sss}_{token}.grib2.bz2"
    )


def _icon_d2_url(cycle: datetime, step: int, var: str) -> str:
    hh = f"{cycle.hour:02d}"
    stamp = cycle.strftime("%Y%m%d%H")
    sss = f"{step:03d}"
    token = "2d_u_10m" if var.startswith("u") else "2d_v_10m"
    return (
        f"{_BASE}/icon-d2/grib/{hh}/{var}/"
        f"icon-d2_germany_regular-lat-lon_single-level_{stamp}_{sss}_{token}.grib2.bz2"
    )


class DwdIconSource(GribSource):
    def __init__(self, spec: SourceSpec, *, kind: str):
        self.spec = spec
        self.kind = kind  # "icon-eu" | "icon-d2"

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]) -> WindField:
        steps = list(steps) or [0]
        url_fn = _icon_eu_url if self.kind == "icon-eu" else _icon_d2_url

        def fetch_one(step: int):
            u_raw = bunzip2(http_get(url_fn(cycle, step, "u_10m")))
            v_raw = bunzip2(http_get(url_fn(cycle, step, "v_10m")))
            la, lo, u2d, te = grib_bytes_to_component(u_raw, which="u")
            _, _, v2d, _ = grib_bytes_to_component(v_raw, which="v")
            return te, u2d, v2d, la, lo

        ordered = map_steps_parallel(steps, fetch_one)
        if not ordered:
            raise RuntimeError(f"{self.kind}: no GRIB steps fetched for cycle {cycle}")

        lats = lons = None
        per_step: list[tuple[float, object, object]] = []
        for _step, payload in ordered:
            te, u2d, v2d, la, lo = payload  # type: ignore[misc]
            if lats is None:
                lats, lons = la, lo
            per_step.append((te, u2d, v2d))

        if not per_step or lats is None or lons is None:
            raise RuntimeError(f"{self.kind}: no GRIB steps fetched for cycle {cycle}")

        field = assemble_windfield(self.spec.model_id, per_step, lats, lons)
        return field.subset_bbox(bbox)


register(
    DwdIconSource(
        SourceSpec(
            model_id="icon_eu",
            connector="dwd-icon",
            resolution_deg=0.0625,
            cycle_hours=(0, 6, 12, 18),
            step_hours=_ICON_EU_STEPS,
            publish_delay_h=4.0,
        ),
        kind="icon-eu",
    )
)

register(
    DwdIconSource(
        SourceSpec(
            model_id="icon_d2",
            connector="dwd-icon",
            resolution_deg=0.02,
            cycle_hours=(0, 3, 6, 9, 12, 15, 18, 21),
            step_hours=_ICON_D2_STEPS,
            publish_delay_h=2.5,
        ),
        kind="icon-d2",
    )
)
