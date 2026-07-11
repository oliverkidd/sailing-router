"""Environment Canada GEM GDPS ~15 km (0.15°) global GRIB source."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ..metocean import WindField
from ..schemas import BBox
from .grib_source import (
    GribSource,
    SourceSpec,
    assemble_windfield,
    grib_bytes_to_component,
    http_get,
    register,
)

logger = logging.getLogger(__name__)

# CMC path layout (lat-lon 0.15). Full global files are large — keep step count tiny.
_BASE = "https://dd.weather.gc.ca/model_gem_global/15km/grib2/lat_lon"
_GEM_STEPS = tuple(range(0, 241, 3))


class GemSource(GribSource):
    def __init__(self, spec: SourceSpec):
        self.spec = spec

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]) -> WindField:
        steps = list(steps) or [0]
        hh = f"{cycle.hour:02d}"
        stamp = cycle.strftime("%Y%m%d%H")

        per_step: list[tuple[float, object, object]] = []
        lats = lons = None
        for step in steps:
            sss = f"{step:03d}"
            u_url = (
                f"{_BASE}/{hh}/{sss}/"
                f"CMC_glb_UGRD_TGL_10_latlon.15x.15_{stamp}_P{sss}.grib2"
            )
            v_url = (
                f"{_BASE}/{hh}/{sss}/"
                f"CMC_glb_VGRD_TGL_10_latlon.15x.15_{stamp}_P{sss}.grib2"
            )
            try:
                u_raw = http_get(u_url)
                v_raw = http_get(v_url)
                la, lo, u2d, te = grib_bytes_to_component(u_raw, which="u")
                _, _, v2d, _ = grib_bytes_to_component(v_raw, which="v")
                if lats is None:
                    lats, lons = la, lo
                per_step.append((te, u2d, v2d))
            except httpx.HTTPStatusError as exc:
                # Cycle/path missing — don't hammer remaining steps.
                logger.warning("GEM step %s HTTP %s — aborting cycle", step, exc.response.status_code)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("GEM step %s failed: %s", step, exc)
                if not per_step:
                    break
                continue

        if not per_step or lats is None or lons is None:
            raise RuntimeError(f"GEM: no steps fetched for cycle {cycle}")

        field = assemble_windfield(self.spec.model_id, per_step, lats, lons)
        return field.subset_bbox(bbox)


register(
    GemSource(
        SourceSpec(
            model_id="gem",
            connector="eccc-gem",
            resolution_deg=0.15,
            cycle_hours=(0, 12),
            step_hours=_GEM_STEPS,
            publish_delay_h=8.0,
        )
    )
)
