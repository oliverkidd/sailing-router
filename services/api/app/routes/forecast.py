"""/wind/grid — LOD wind tile overlay endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from ..config import MODEL_PRESETS_BY_ID
from ..metocean_service import load_or_fetch_wind_tile
from ..overlays.grid import wind_grid_payload
from ..schemas import BBox
from ..util.bbox import TILE_N, align_tile_bbox, clamp_grid_dims
from ..util.timeparse import parse_utc_iso

router = APIRouter(tags=["forecast"])


@router.get("/wind/grid")
def wind_grid(
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
    t_utc: str | None = None,
    model_id: str = "ecmwf",
    nx: int = TILE_N,
    ny: int = TILE_N,
) -> dict:
    """Return a single-time wind tile at explicit dims (LOD pyramid building block)."""
    bbox = align_tile_bbox(BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max))
    preset = MODEL_PRESETS_BY_ID.get(model_id)
    res = preset.resolution_deg if preset else 0.25
    nlat, nlon = clamp_grid_dims(bbox, nx, ny, res)
    t = parse_utc_iso(t_utc) if t_utc else datetime.now(UTC)

    field, err = load_or_fetch_wind_tile(model_id, bbox, nlat=nlat, nlon=nlon)
    if field is None:
        return {
            "error": "fetch_failed",
            "message": err or "Open-Meteo forecast request failed",
            "bbox": bbox.model_dump(),
        }
    return wind_grid_payload(
        field,
        bbox.lat_min,
        bbox.lat_max,
        bbox.lon_min,
        bbox.lon_max,
        t,
        nlat=nlat,
        nlon=nlon,
    )
