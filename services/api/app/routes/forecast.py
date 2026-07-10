"""/forecasts/fetch (M2) + /currents/fetch (M5).

Local-first: fetch while connected, cache to disk, then route offline.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import cache
from ..config import MODEL_PRESETS_BY_ID
from ..forecast import get_provider
from ..schemas import (
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
