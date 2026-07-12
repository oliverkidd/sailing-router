"""Environment Canada GEM / GDPS ~15 km (0.15°) global GRIB source.

MSC Datamart renamed the product from ``model_gem_global`` → ``model_gdps`` and
switched filenames to the MSC_GDPS WindU/WindV convention. Current files live
under ``/today/model_gdps/15km/{HH}/{step}/`` (dated mirror:
``/{YYYYMMDD}/WXO-DD/model_gdps/...``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

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

# Hourly to 84 h, then 3-hourly to 240 h (MSC GDPS Datamart cadence).
_GEM_STEPS = tuple(list(range(0, 85, 1)) + list(range(87, 241, 3)))


def _gem_root(cycle: datetime, now: datetime | None = None) -> str:
    """Prefer /today/ for the current UTC day; else the dated WXO-DD mirror."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if cycle.tzinfo is None:
        cycle = cycle.replace(tzinfo=UTC)
    if cycle.astimezone(UTC).date() == now.astimezone(UTC).date():
        return "https://dd.weather.gc.ca/today/model_gdps/15km"
    day = cycle.astimezone(UTC).strftime("%Y%m%d")
    return f"https://dd.weather.gc.ca/{day}/WXO-DD/model_gdps/15km"


def _gem_urls(cycle: datetime, step: int) -> tuple[str, str]:
    hh = f"{cycle.hour:02d}"
    sss = f"{step:03d}"
    stamp = cycle.strftime("%Y%m%d") + f"T{hh}Z"
    root = _gem_root(cycle)
    base = f"{root}/{hh}/{sss}"
    u = f"{base}/{stamp}_MSC_GDPS_WindU_AGL-10m_LatLon0.15_PT{sss}H.grib2"
    v = f"{base}/{stamp}_MSC_GDPS_WindV_AGL-10m_LatLon0.15_PT{sss}H.grib2"
    return u, v


class GemSource(GribSource):
    def __init__(self, spec: SourceSpec):
        self.spec = spec

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]) -> WindField:
        steps = list(steps) or [0]

        per_step: list[tuple[float, object, object]] = []
        lats = lons = None
        for step in steps:
            u_url, v_url = _gem_urls(cycle, step)
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
