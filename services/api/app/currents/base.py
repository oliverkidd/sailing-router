"""CurrentProvider interface. All implementations are European (MED / NW-Shelf / FES)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..metocean import CurrentField
from ..schemas import BBox


class CurrentProvider(ABC):
    @abstractmethod
    def fetch(self, bbox: BBox, start: datetime, end: datetime) -> CurrentField:
        """Return a CurrentField (time, lat, lon; u, v m/s) over the window."""
        raise NotImplementedError
