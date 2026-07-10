"""European forecast connectors (pluggable behind ForecastProvider)."""

import os

from .base import ForecastProvider
from .dwd_icon import DWDIconProvider
from .ecmwf import ECMWFOpenDataProvider
from .meteofrance import MeteoFranceProvider
from .openmeteo import OpenMeteoProvider

__all__ = [
    "ForecastProvider",
    "OpenMeteoProvider",
    "DWDIconProvider",
    "ECMWFOpenDataProvider",
    "MeteoFranceProvider",
    "get_provider",
]

# Opt-in native GRIB path; default is the plug-and-play Open-Meteo connector.
_USE_NATIVE = os.environ.get("SAILING_FORECAST_NATIVE") == "1"

_NATIVE = {
    "dwd": DWDIconProvider,
    "ecmwf-open": ECMWFOpenDataProvider,
    "meteofrance": MeteoFranceProvider,
}


def get_provider(connector: str) -> ForecastProvider:
    """Return a provider for a connector id. Region-locked: European only.

    Default: Open-Meteo (one endpoint, no key, no US infra). With
    SAILING_FORECAST_NATIVE=1 and the optional metocean extras installed, native
    European GRIB connectors are used where available.
    """
    if connector not in ("open-meteo", "dwd", "ecmwf-open", "meteofrance"):
        raise ValueError(f"Unknown/forbidden forecast connector: {connector}")
    if _USE_NATIVE and connector in _NATIVE:
        return _NATIVE[connector]()
    return OpenMeteoProvider()
