"""/forecasts/fetch (M2) + /currents/fetch (M5).

Local-first: fetch while connected, cache to disk, then route offline.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from .. import cache
from ..config import MODEL_PRESETS_BY_ID
from ..currents import get_current_provider
from ..forecast import get_provider
from ..metocean import MS_TO_KN
from ..schemas import (
    BBox,
    CurrentFetchRequest,
    CurrentFetchResponse,
    ForecastFetchRequest,
    ForecastFetchResponse,
    ForecastModelStatus,
)

router = APIRouter(tags=["forecast"])


@router.post("/forecasts/fetch", response_model=ForecastFetchResponse)
def fetch_forecasts(req: ForecastFetchRequest) -> ForecastFetchResponse:
    results: list[ForecastModelStatus] = []
    for model_id in req.model_ids:
        preset = MODEL_PRESETS_BY_ID.get(model_id)
        if preset is None:
            results.append(
                ForecastModelStatus(
                    model_id=model_id, status="error", message="Unknown model preset"
                )
            )
            continue

        key = cache.wind_key(model_id, req.bbox, req.cycle)
        if cache.has_wind(key):
            field = cache.load_wind(key)
            results.append(
                ForecastModelStatus(
                    model_id=model_id, status="cached", cache_key=key, n_times=field.n_times
                )
            )
            continue

        try:
            provider = get_provider(preset.connector)
            field = provider.fetch(model_id, req.bbox, req.cycle, req.horizon_h)
            cache.save_wind(key, field)
            results.append(
                ForecastModelStatus(
                    model_id=model_id, status="fetched", cache_key=key, n_times=field.n_times
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                ForecastModelStatus(model_id=model_id, status="error", message=str(exc))
            )

    return ForecastFetchResponse(results=results)


@router.post("/currents/fetch", response_model=CurrentFetchResponse)
def fetch_currents(req: CurrentFetchRequest) -> CurrentFetchResponse:
    key = cache.current_key(req.source, req.bbox, req.window_start_utc.isoformat())
    if cache.has_current(key):
        return CurrentFetchResponse(status="cached", cache_key=key, source=req.source)

    # Try the requested source; fall back to FES (offline) so routing always works.
    for source in (req.source, "fes"):
        try:
            provider = get_current_provider(source)
            field = provider.fetch(req.bbox, req.window_start_utc, req.window_end_utc)
            save_key = cache.current_key(source, req.bbox, req.window_start_utc.isoformat())
            cache.save_current(save_key, field)
            msg = None if source == req.source else f"{req.source} unavailable; used FES offline"
            return CurrentFetchResponse(
                status="fetched", cache_key=save_key, source=source, message=msg
            )
        except Exception:  # noqa: BLE001
            continue

    return CurrentFetchResponse(
        status="error", source=req.source, message="No current source available"
    )


@router.get("/currents/arrows")
def current_arrows(
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
    t_utc: str | None = None,
    source: str = "fes",
) -> dict:
    """Sample the current field on a coarse grid and return arrow segments (GeoJSON).

    Arrows point along the current set (direction toward); length scales with rate.
    """
    bbox = BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    t = datetime.fromisoformat(t_utc).replace(tzinfo=UTC) if t_utc else datetime.now(UTC)
    try:
        field = get_current_provider(source).fetch(bbox, t, t + timedelta(hours=6))
    except Exception:  # noqa: BLE001
        return {"type": "FeatureCollection", "features": []}

    feats = []
    for la in field.lats[::1]:
        for lo in field.lons[::1]:
            u, v = field.current_at(float(la), float(lo), t)
            rate_kn = math.hypot(u, v) * MS_TO_KN
            if rate_kn < 0.02:
                continue
            # Short vector; ~0.03 deg per knot for visibility.
            scale = 0.03
            end_lat = float(la) + v * MS_TO_KN * scale * 0.5
            end_lon = float(lo) + u * MS_TO_KN * scale * 0.5 / max(0.1, math.cos(math.radians(float(la))))
            feats.append(
                {
                    "type": "Feature",
                    "properties": {"rate_kn": round(rate_kn, 2)},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[float(lo), float(la)], [end_lon, end_lat]],
                    },
                }
            )
    return {"type": "FeatureCollection", "features": feats}
