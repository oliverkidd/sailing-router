"""NOAA GFS 0.25° GRIB source via AWS open data (idx byte-range)."""

from __future__ import annotations

import logging
from datetime import datetime

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

        per_step: list[tuple[float, object, object]] = []
        lats = lons = None
        consecutive_fail = 0
        for step in steps:
            fname = f"gfs.t{hh}z.pgrb2.0p25.f{step:03d}"
            url = f"{base}/{fname}"
            try:
                idx_text = http_get(f"{url}.idx").decode("utf-8", errors="replace")
                records = parse_idx(idx_text)
                u_range = find_idx_range(records, ":UGRD:10 m above ground:")
                v_range = find_idx_range(records, ":VGRD:10 m above ground:")
                if u_range is None or v_range is None:
                    raise RuntimeError(f"GFS idx missing 10m U/V for {fname}")
                u_start, u_end = u_range
                v_start, v_end = v_range
                if u_end < 0 or v_end < 0:
                    raise RuntimeError(f"GFS idx open-ended range for {fname}")
                u_raw = http_get_range(url, u_start, u_end)
                v_raw = http_get_range(url, v_start, v_end)

                la, lo, u2d, te = grib_bytes_to_component(u_raw, which="u")
                _, _, v2d, _ = grib_bytes_to_component(v_raw, which="v")
                if lats is None:
                    lats, lons = la, lo
                per_step.append((te, u2d, v2d))
                consecutive_fail = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_fail += 1
                logger.warning("GFS step %s failed: %s", step, exc)
                if consecutive_fail >= 2 and not per_step:
                    raise RuntimeError(
                        f"GFS: first steps failed for cycle {cycle} ({exc})"
                    ) from exc
                if consecutive_fail >= 3:
                    break
                continue

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
