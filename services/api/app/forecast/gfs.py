"""NOAA GFS 0.25° GRIB source via AWS open data (idx byte-range)."""

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
    find_idx_range,
    grib_bytes_to_component,
    http_get,
    http_get_range,
    map_steps_parallel,
    parse_idx,
    register,
)

logger = logging.getLogger(__name__)

_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"

# Prefer finest native cadence: hourly to 120h, then 3-hourly to 240h.
_GFS_STEPS = tuple(list(range(0, 121, 1)) + list(range(123, 241, 3)))


class GfsSource(GribSource):
    def __init__(self, spec: SourceSpec):
        self.spec = spec

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]) -> WindField:
        steps = list(steps) or [0]
        day = cycle.strftime("%Y%m%d")
        hh = f"{cycle.hour:02d}"
        base = f"{_BASE}/gfs.{day}/{hh}/atmos"

        def fetch_one(step: int):
            fname = f"gfs.t{hh}z.pgrb2.0p25.f{step:03d}"
            url = f"{base}/{fname}"
            with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                idx_text = http_get(f"{url}.idx", client=client).decode(
                    "utf-8", errors="replace"
                )
                records = parse_idx(idx_text)
                u_range = find_idx_range(records, ":UGRD:10 m above ground:")
                v_range = find_idx_range(records, ":VGRD:10 m above ground:")
                if u_range is None or v_range is None:
                    raise RuntimeError(f"GFS idx missing 10m U/V for {fname}")
                u_start, u_end = u_range
                v_start, v_end = v_range
                if u_end < 0 or v_end < 0:
                    raise RuntimeError(f"GFS idx open-ended range for {fname}")
                u_raw = http_get_range(url, u_start, u_end, client=client)
                v_raw = http_get_range(url, v_start, v_end, client=client)

            la, lo, u2d, te = grib_bytes_to_component(u_raw, which="u")
            _, _, v2d, _ = grib_bytes_to_component(v_raw, which="v")
            return te, u2d, v2d, la, lo

        ordered = map_steps_parallel(steps, fetch_one)
        if not ordered:
            raise RuntimeError(f"GFS: no steps fetched for cycle {cycle}")

        lats = lons = None
        per_step: list[tuple[float, object, object]] = []
        for _step, payload in ordered:
            te, u2d, v2d, la, lo = payload  # type: ignore[misc]
            if lats is None:
                lats, lons = la, lo
            per_step.append((te, u2d, v2d))

        if not per_step or lats is None or lons is None:
            raise RuntimeError(f"GFS: no steps fetched for cycle {cycle}")

        field = assemble_windfield(self.spec.model_id, per_step, lats, lons)
        return field.subset_bbox(bbox)


register(
    GfsSource(
        SourceSpec(
            model_id="gfs",
            connector="noaa-gfs",
            resolution_deg=0.25,
            cycle_hours=(0, 6, 12, 18),
            step_hours=_GFS_STEPS,
            publish_delay_h=5.0,
        )
    )
)
