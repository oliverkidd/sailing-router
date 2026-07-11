"""/wind/area — multi-hour display series for viewport overlays + playback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from ..field_service import get_display_series
from ..schemas import BBox
from ..util.timeparse import parse_utc_iso

router = APIRouter(tags=["forecast"])


@router.get("/wind/area")
def wind_area(
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
    model_id: str = "ecmwf",
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict:
    """Return a multi-hour wind series for the viewport (canonical + display budget)."""
    bbox = BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    now = datetime.now(UTC)
    start = parse_utc_iso(start_utc) if start_utc else now
    end = parse_utc_iso(end_utc) if end_utc else start + timedelta(hours=24)

    payload = get_display_series(model_id, bbox, start=start, end=end)
    if payload is None:
        return {
            "error": "unavailable",
            "message": f"{model_id} wind unavailable",
            "bbox": bbox.model_dump(),
        }
    return payload
