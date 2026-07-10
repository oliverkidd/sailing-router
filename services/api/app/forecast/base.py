"""ForecastProvider interface. All implementations are European (no US feeds)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..metocean import WindField
from ..schemas import BBox


class ForecastProvider(ABC):
    """A pluggable wind source. Swapping a source never touches the router."""

    @abstractmethod
    def fetch(
        self,
        model_id: str,
        bbox: BBox,
        cycle: str | None,
        horizon_h: int,
    ) -> WindField:
        """Return a canonical WindField (time, lat, lon; u10, v10 m/s) for the bbox."""
        raise NotImplementedError
