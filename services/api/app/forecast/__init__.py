"""Forecast connectors — GRIB-only. Open-Meteo removed."""

from __future__ import annotations

from .grib_source import GribSource, get_source, register_all

__all__ = [
    "GribSource",
    "get_source",
    "register_all",
]

# Register all adapters on import.
register_all()
