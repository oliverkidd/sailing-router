"""Configuration: model presets, connector wiring, and runtime paths.

Region lock: UK + Mediterranean only. NO US systems (no NOAA/NOMADS/GFS/HRRR).
"""

from __future__ import annotations

import os
from pathlib import Path

from .schemas import ModelPreset

# --- Paths ------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("SAILING_DATA_DIR", Path(__file__).resolve().parents[3] / "data"))
CACHE_DIR = Path(os.environ.get("SAILING_CACHE_DIR", DATA_DIR / "cache"))
FIXTURES_DIR = DATA_DIR / "fixtures"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- UK + Mediterranean region bbox (for the default map view) --------------

REGION_BBOX = {
    "lat_min": 30.0,
    "lon_min": -12.0,
    "lat_max": 62.0,
    "lon_max": 37.0,
}

# --- Forecast model presets (European only) ---------------------------------
# Colors picked for distinct, PredictWind-style overlay. Mirrored in
# apps/web/src/lib/colors.ts.

MODEL_PRESETS: list[ModelPreset] = [
    ModelPreset(
        id="ecmwf",
        label="ECMWF IFS",
        resolution_deg=0.25,
        connector="open-meteo",
        color="#2dd4bf",  # teal
    ),
    ModelPreset(
        id="icon",
        label="ICON Global (DWD)",
        resolution_deg=0.13,
        connector="open-meteo",
        color="#f59e0b",  # amber
    ),
    ModelPreset(
        id="icon_eu",
        label="ICON-EU (DWD)",
        resolution_deg=0.0625,
        connector="open-meteo",
        color="#a78bfa",  # violet
    ),
    ModelPreset(
        id="arpege",
        label="ARPEGE (Météo-France)",
        resolution_deg=0.1,
        connector="open-meteo",
        color="#f472b6",  # pink
    ),
    ModelPreset(
        id="arome",
        label="AROME (Météo-France)",
        resolution_deg=0.025,
        connector="open-meteo",
        color="#4ade80",  # green
    ),
    ModelPreset(
        id="ukmo",
        label="UKMO Global",
        resolution_deg=0.09,
        connector="open-meteo",
        color="#60a5fa",  # blue
    ),
]

MODEL_PRESETS_BY_ID = {m.id: m for m in MODEL_PRESETS}

# Map our preset ids -> Open-Meteo `models=` values. No US models.
OPEN_METEO_MODEL_MAP = {
    "ecmwf": "ecmwf_ifs025",
    "icon": "icon_global",
    "icon_eu": "icon_eu",
    "arpege": "meteofrance_arpege_world",
    "arome": "meteofrance_arome_france",
    "ukmo": "ukmo_global_deterministic_10km",
}

# --- Terrain-wind engine ----------------------------------------------------

TERRAIN_WIND_ENGINE = os.environ.get("SAILING_TERRAIN_WIND_ENGINE", "windninja")  # windninja|heuristic|none
WINDNINJA_BIN = os.environ.get("SAILING_WINDNINJA_BIN", "WindNinja_cli")
