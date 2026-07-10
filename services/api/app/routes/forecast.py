"""/forecasts/fetch + /currents/fetch endpoints. Filled in at M2 / M5."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["forecast"])
