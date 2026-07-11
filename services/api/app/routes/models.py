"""Model catalog + geographic availability."""

from __future__ import annotations

from fastapi import APIRouter

from ..config import MODEL_PRESETS, models_available_for_bbox
from ..schemas import BBox, ModelPreset

router = APIRouter(tags=["models"])


@router.get("/models", response_model=list[ModelPreset])
def get_models() -> list[ModelPreset]:
    return MODEL_PRESETS


@router.get("/models/available", response_model=list[ModelPreset])
def get_models_available(
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
) -> list[ModelPreset]:
    """Return models whose static coverage contains the given bbox. No network calls."""
    bbox = BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    return models_available_for_bbox(bbox)
