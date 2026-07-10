"""ECMWF open-data native GRIB connector (optional).

Downloads only 10 m U/V for the bbox via `ecmwf-opendata` and reads with
cfgrib/xarray. European IFS only — never NOAA/NOMADS. Enabled when the optional
`metocean` extras are installed and SAILING_FORECAST_NATIVE is set; otherwise the
Open-Meteo connector is used.
"""

from __future__ import annotations

from ..metocean import WindField
from ..schemas import BBox
from .base import ForecastProvider


class ECMWFOpenDataProvider(ForecastProvider):
    def fetch(self, model_id: str, bbox: BBox, cycle: str | None, horizon_h: int) -> WindField:
        try:
            import cfgrib  # noqa: PLC0415, F401
            from ecmwf.opendata import Client  # noqa: PLC0415, F401
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "ecmwf-opendata/cfgrib not installed; use the Open-Meteo connector"
            ) from exc
        # TODO(v2): retrieve 10u/10v GRIB2 for the bbox+cycle, read with cfgrib,
        # subset to bbox, and assemble a canonical WindField (u10, v10 m/s).
        raise NotImplementedError("Native ECMWF GRIB path not wired (use Open-Meteo)")
