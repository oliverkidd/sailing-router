"""/boat endpoint: upload a polar CSV + BoatConfig, get a validated BoatConfig back.

Filled in at M1.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["boat"])
