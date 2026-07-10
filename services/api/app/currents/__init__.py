"""European current/tide connectors (pluggable behind CurrentProvider)."""

from .base import CurrentProvider
from .copernicus import CopernicusCurrentProvider
from .fes import FESCurrentProvider

__all__ = [
    "CurrentProvider",
    "CopernicusCurrentProvider",
    "FESCurrentProvider",
    "get_current_provider",
]


def get_current_provider(source: str) -> CurrentProvider:
    """Return a current provider for a source id.

    - copernicus-smoc / copernicus-med: Copernicus Marine (free account).
    - fes: FES2014/2022 harmonic tides, fully offline (default fallback).
    """
    if source == "fes":
        return FESCurrentProvider()
    if source in ("copernicus-smoc", "copernicus-med"):
        return CopernicusCurrentProvider(product=source)
    raise ValueError(f"Unknown current source: {source}")
