"""/elevation — DEM / depth / land lookup at a map click."""

from __future__ import annotations

from fastapi import APIRouter

from ..geo.fetch import depth_at_point, depth_lt_at_point, elevation_at_point, on_land_at_point
from ..schemas import DemCoverageResponse, DemFootprint, ElevationResponse
from ..terrain import get_terrain

router = APIRouter(tags=["geo"])


@router.get("/elevation", response_model=ElevationResponse)
def elevation(lat: float, lon: float) -> ElevationResponse:
    depth_m = depth_at_point(lat, lon)
    on_land = on_land_at_point(lat, lon)
    terrain = get_terrain()
    elevation_m = elevation_at_point(lat, lon)
    return ElevationResponse(
        lat=lat,
        lon=lon,
        elevation_m=elevation_m,
        depth_m=depth_m,
        depth_lt_m=depth_lt_at_point(
            lat,
            lon,
            on_land=on_land,
            depth_m=depth_m,
            elevation_m=elevation_m,
        ),
        on_land=on_land,
        dem_available=terrain.covers(lat, lon) if terrain.available else False,
    )


@router.get("/dem/coverage", response_model=DemCoverageResponse)
def dem_coverage() -> DemCoverageResponse:
    """Bounding boxes of every open DEM mosaic/tile (for map debug outlines)."""
    terrain = get_terrain()
    return DemCoverageResponse(
        footprints=[DemFootprint(**fp) for fp in terrain.footprints()],
    )
