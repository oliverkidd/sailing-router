"""Météo-France open-data native GRIB connector (optional) — ARPEGE / AROME.

Free (token). AROME's high-res domain centers on France/W-Med; UK is covered by
ECMWF/ICON/UKMO. Reads 10 m U/V GRIB2 with cfgrib/xarray. Falls back to Open-Meteo
when optional deps/token aren't available.
"""

from __future__ import annotations

from ..metocean import WindField
from ..schemas import BBox
from .base import ForecastProvider


class MeteoFranceProvider(ForecastProvider):
    def fetch(self, model_id: str, bbox: BBox, cycle: str | None, horizon_h: int) -> WindField:
        try:
            import cfgrib  # noqa: PLC0415, F401
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("cfgrib not installed; use the Open-Meteo connector") from exc
        # TODO(v2): fetch ARPEGE/AROME 10u/10v GRIB2 (token), read with cfgrib,
        # subset to bbox, assemble a canonical WindField.
        raise NotImplementedError("Native Météo-France GRIB path not wired (use Open-Meteo)")
