"""DWD Open Data native GRIB connector (optional) — ICON / ICON-EU.

Free, no key. Downloads 10 m U/V GRIB2 for the bbox and reads with cfgrib/xarray.
European only. Falls back to Open-Meteo when the optional deps aren't installed.
"""

from __future__ import annotations

from ..metocean import WindField
from ..schemas import BBox
from .base import ForecastProvider


class DWDIconProvider(ForecastProvider):
    def fetch(self, model_id: str, bbox: BBox, cycle: str | None, horizon_h: int) -> WindField:
        try:
            import cfgrib  # noqa: PLC0415, F401
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("cfgrib not installed; use the Open-Meteo connector") from exc
        # TODO(v2): fetch ICON/ICON-EU 10u/10v GRIB2 from opendata.dwd.de, read with
        # cfgrib, subset to bbox, assemble a canonical WindField.
        raise NotImplementedError("Native DWD GRIB path not wired (use Open-Meteo)")
