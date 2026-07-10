"""European forecast connectors (pluggable behind ForecastProvider)."""

from .base import ForecastProvider
from .openmeteo import OpenMeteoProvider

__all__ = ["ForecastProvider", "OpenMeteoProvider", "get_provider"]


def get_provider(connector: str) -> ForecastProvider:
    """Return a provider instance for a connector id. Region-locked: European only.

    Native GRIB connectors (dwd, ecmwf-open, meteofrance) are optional and added
    in M7; until then everything routes through Open-Meteo.
    """
    if connector in ("open-meteo", "dwd", "ecmwf-open", "meteofrance"):
        # TODO(v2): return native GRIB providers for dwd/ecmwf-open/meteofrance.
        return OpenMeteoProvider()
    raise ValueError(f"Unknown/forbidden forecast connector: {connector}")
