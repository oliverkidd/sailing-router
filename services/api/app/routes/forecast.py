"""/wind/area — multi-hour display series for viewport overlays + playback."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Response

from ..config import WIND_TTL_S
from ..field_service import get_display_series
from ..forecast.grib_source import ForecastRateLimitError
from ..schemas import BBox
from ..util.timeparse import parse_utc_iso

router = APIRouter(tags=["forecast"])


@router.get("/wind/area")
def wind_area(
    response: Response,
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
    model_id: str = "ecmwf",
    start_utc: str | None = None,
    end_utc: str | None = None,
    zoom: float | None = None,
    terrain: bool = True,
) -> dict:
    """Return a multi-hour wind series for the viewport (WindProvider path).

    Client should send the raw viewport bbox; the server pads once for the
    forcing cache and returns a zoom-tiered display window (rendered fully).

    ``terrain`` toggles WindNinja terrain-response downscaling (default on).
    """
    bbox = BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    now = datetime.now(UTC)
    start = parse_utc_iso(start_utc) if start_utc else now
    end = parse_utc_iso(end_utc) if end_utc else start + timedelta(hours=24)

    # Cache headers — browser may reuse within the forecast TTL.
    etag_src = (
        f"{model_id}|{lat_min:.4f},{lon_min:.4f},{lat_max:.4f},{lon_max:.4f}|"
        f"{start.isoformat()}|{end.isoformat()}|{zoom}|t={int(terrain)}"
    )
    etag = hashlib.sha1(etag_src.encode()).hexdigest()[:16]
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Cache-Control"] = f"public, max-age={int(WIND_TTL_S)}"

    try:
        payload = get_display_series(
            model_id,
            bbox,
            start=start,
            end=end,
            zoom=zoom,
            enable_terrain=terrain,
        )
    except ForecastRateLimitError as exc:
        response.headers["Cache-Control"] = "no-store"
        return {
            "error": "rate_limited",
            "message": str(exc),
            "bbox": bbox.model_dump(),
        }
    if payload is None:
        response.headers["Cache-Control"] = "no-store"
        return {
            "error": "unavailable",
            "message": f"{model_id} wind unavailable",
            "bbox": bbox.model_dump(),
        }
    return payload
