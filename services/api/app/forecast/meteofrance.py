"""Météo-France AROME / ARPEGE-EU GRIB sources (requires METEOFRANCE_API_KEY)."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from ..metocean import WindField
from ..schemas import BBox
from .grib_source import (
    GribSource,
    SourceSpec,
    assemble_windfield,
    grib_bytes_to_uv,
    http_get,
    map_steps_parallel,
    register,
)

logger = logging.getLogger(__name__)

# Portail API — coverage packages for AROME and ARPEGE Europe.
_MF_BASE = "https://public-api.meteofrance.fr/public"
_AROME_STEPS = tuple(range(0, 49, 1))
_ARPEGE_STEPS = tuple(range(0, 97, 1))


def _mf_headers() -> dict[str, str]:
    key = os.environ.get("METEOFRANCE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("METEOFRANCE_API_KEY not set")
    return {"apikey": key}


class MeteoFranceSource(GribSource):
    def __init__(self, spec: SourceSpec, *, package: str, model_path: str):
        self.spec = spec
        self.package = package
        self.model_path = model_path

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]) -> WindField:
        steps = list(steps) or [0]
        headers = _mf_headers()
        stamp = cycle.strftime("%Y-%m-%dT%H:00:00Z")

        def fetch_one(step: int):
            url = (
                f"{_MF_BASE}/{self.model_path}/1.0/wcs/"
                f"{self.package}"
                f"?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
                f"&format=application/wmo-grib"
                f"&coverageId=TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"
                f"&subset=time({stamp})"
                f"&subset=lat({bbox.lat_min},{bbox.lat_max})"
                f"&subset=long({bbox.lon_min},{bbox.lon_max})"
                f"&subset=forecast_hour({step})"
            )
            u_url = url.replace(
                "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND",
                "U_COMPONENT_OF_WIND__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND",
            )
            v_url = url.replace(
                "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND",
                "V_COMPONENT_OF_WIND__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND",
            )
            u_raw = http_get(u_url, headers=headers)
            v_raw = http_get(v_url, headers=headers)
            la, lo, u2d, _, te = grib_bytes_to_uv(u_raw)
            _, _, _, v2d, _ = grib_bytes_to_uv(v_raw)
            return te, u2d, v2d, la, lo

        ordered = map_steps_parallel(steps, fetch_one)
        if not ordered:
            raise RuntimeError(f"{self.spec.model_id}: no GRIB steps fetched")

        lats = lons = None
        per_step: list[tuple[float, object, object]] = []
        for _step, payload in ordered:
            te, u2d, v2d, la, lo = payload  # type: ignore[misc]
            if lats is None:
                lats, lons = la, lo
            per_step.append((te, u2d, v2d))

        if not per_step or lats is None or lons is None:
            raise RuntimeError(f"{self.spec.model_id}: no GRIB steps fetched")

        field = assemble_windfield(self.spec.model_id, per_step, lats, lons)
        return field.subset_bbox(bbox)


# Only register when key is present so unconfigured models stay hidden.
if os.environ.get("METEOFRANCE_API_KEY", "").strip():
    register(
        MeteoFranceSource(
            SourceSpec(
                model_id="arome",
                connector="mf-arome",
                resolution_deg=0.025,
                cycle_hours=(0, 3, 6, 9, 12, 15, 18, 21),
                step_hours=_AROME_STEPS,
                publish_delay_h=2.0,
                api_key_env="METEOFRANCE_API_KEY",
            ),
            package="AROME_SURFACE",
            model_path="arome/1.0",
        )
    )
    register(
        MeteoFranceSource(
            SourceSpec(
                model_id="arpege_eu",
                connector="mf-arpege",
                resolution_deg=0.1,
                cycle_hours=(0, 6, 12, 18),
                step_hours=_ARPEGE_STEPS,
                publish_delay_h=4.0,
                api_key_env="METEOFRANCE_API_KEY",
            ),
            package="ARPEGE_EUROPE",
            model_path="arpege/1.0",
        )
    )
