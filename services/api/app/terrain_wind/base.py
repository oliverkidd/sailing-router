"""TerrainWindModel interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..geo import LandMask
from ..metocean import WindField
from ..terrain import TerrainProvider


class TerrainWindModel(ABC):
    @abstractmethod
    def apply(
        self,
        wind: WindField,
        land: LandMask,
        terrain: TerrainProvider,
        refine_n: int = 24,
    ) -> WindField:
        """Return a terrain-downscaled WindField (corner speed-up, lee shadow, etc.)."""
        raise NotImplementedError
