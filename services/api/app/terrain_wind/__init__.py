"""Orographic terrain-wind downscaling (the accuracy layer).

Engines (config SAILING_TERRAIN_WIND_ENGINE):
- windninja: WindNinja OSS CLI (mass-conserving + diurnal). Best accuracy.
- heuristic: no-dependency geometric shadow/corner model (fallback / fast preview).
- none: pass-through (no terrain effect).
"""

from .base import TerrainWindModel
from .heuristic import HeuristicTerrainWind
from .windninja import WindNinjaTerrainWind

__all__ = ["TerrainWindModel", "HeuristicTerrainWind", "WindNinjaTerrainWind", "get_terrain_wind"]


def get_terrain_wind(engine: str) -> TerrainWindModel | None:
    if engine == "none":
        return None
    if engine == "heuristic":
        return HeuristicTerrainWind()
    if engine == "windninja":
        # Falls back to the heuristic internally if the binary is missing.
        return WindNinjaTerrainWind(fallback=HeuristicTerrainWind())
    raise ValueError(f"Unknown terrain-wind engine: {engine}")
