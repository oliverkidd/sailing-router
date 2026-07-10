"""/elevation endpoint + depth-contour GeoJSON for the map."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from ..config import DATA_DIR
from ..schemas import ElevationResponse
from ..terrain import get_terrain

router = APIRouter(tags=["geo"])

_CONTOURS = DATA_DIR / "masks" / "depth_contours.geojson"
_LAND = DATA_DIR / "masks" / "land_aegean.geojson"


@router.get("/elevation", response_model=ElevationResponse)
def elevation(lat: float, lon: float) -> ElevationResponse:
    val = get_terrain().elevation(lat, lon)
    return ElevationResponse(lat=lat, lon=lon, elevation_m=val)


@router.get("/depth/contours")
def depth_contours() -> dict:
    """Prebuilt EMODnet depth contours (0/3/5/10 m) as GeoJSON, if available."""
    p = Path(_CONTOURS)
    if p.exists():
        return json.loads(p.read_text())
    return {"type": "FeatureCollection", "features": []}


@router.get("/land/mask")
def land_mask() -> dict:
    """Prebuilt coastline polygons (advisory, not nav-grade) as GeoJSON."""
    p = Path(_LAND)
    if p.exists():
        return json.loads(p.read_text())
    return {"type": "FeatureCollection", "features": []}
