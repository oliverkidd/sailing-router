"""Configuration: model presets, connector wiring, and runtime paths.

Global forecast models via Open-Meteo; regional high-res models where available.
"""

from __future__ import annotations

import os
from pathlib import Path

from .schemas import BBox, ModelPreset

# --- Paths ------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("SAILING_DATA_DIR", Path(__file__).resolve().parents[3] / "data"))
CACHE_DIR = Path(os.environ.get("SAILING_CACHE_DIR", DATA_DIR / "cache"))
GEO_CACHE_DIR = CACHE_DIR / "geo"
FIXTURES_DIR = DATA_DIR / "fixtures"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
GEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- Forecast model presets (Windy/PredictWind-style list) ------------------

MODEL_PRESETS: list[ModelPreset] = [
    # Global models
    ModelPreset(
        id="gfs",
        label="GFS",
        resolution_deg=0.11,
        connector="open-meteo",
        color="#ef4444",
    ),
    ModelPreset(
        id="ecmwf",
        label="ECMWF IFS",
        resolution_deg=0.25,
        connector="open-meteo",
        color="#2dd4bf",
    ),
    ModelPreset(
        id="icon",
        label="ICON Global",
        resolution_deg=0.13,
        connector="open-meteo",
        color="#f59e0b",
    ),
    ModelPreset(
        id="ukmo",
        label="UKMO Global",
        resolution_deg=0.09,
        connector="open-meteo",
        color="#60a5fa",
    ),
    ModelPreset(
        id="gem",
        label="GEM Global",
        resolution_deg=0.15,
        connector="open-meteo",
        color="#a78bfa",
    ),
    # Regional high-res
    ModelPreset(
        id="icon_eu",
        label="ICON-EU",
        resolution_deg=0.0625,
        connector="open-meteo",
        color="#fbbf24",
    ),
    ModelPreset(
        id="icon_d2",
        label="ICON-D2",
        resolution_deg=0.02,
        connector="open-meteo",
        color="#fcd34d",
    ),
    ModelPreset(
        id="arome",
        label="AROME France HD",
        resolution_deg=0.025,
        connector="open-meteo",
        color="#f472b6",
    ),
    ModelPreset(
        id="arpege",
        label="ARPEGE World",
        resolution_deg=0.1,
        connector="open-meteo",
        color="#fb7185",
    ),
    ModelPreset(
        id="arpege_eu",
        label="ARPEGE EU",
        resolution_deg=0.05,
        connector="open-meteo",
        color="#fda4af",
    ),
    ModelPreset(
        id="harmonie",
        label="Harmonie (KNMI)",
        resolution_deg=0.02,
        connector="open-meteo",
        color="#38bdf8",
    ),
]

MODEL_PRESETS_BY_ID = {m.id: m for m in MODEL_PRESETS}

# Global vs regional grouping for UI
MODEL_GROUPS: dict[str, list[str]] = {
    "global": ["gfs", "ecmwf", "icon", "ukmo", "gem", "arpege"],
    "regional": ["icon_eu", "icon_d2", "arome", "arpege_eu", "harmonie"],
}

# Geographic coverage. None = global. Approximate Open-Meteo domains.
MODEL_COVERAGE: dict[str, BBox | None] = {
    "gfs": None,
    "ecmwf": None,
    "icon": None,
    "ukmo": None,
    "gem": None,
    "arpege": None,
    "icon_eu": BBox(lat_min=29.5, lon_min=-23.5, lat_max=70.5, lon_max=45.0),
    "arpege_eu": BBox(lat_min=20.0, lon_min=-32.0, lat_max=72.0, lon_max=42.0),
    "harmonie": BBox(lat_min=39.0, lon_min=-25.0, lat_max=62.0, lon_max=30.0),
    "icon_d2": BBox(lat_min=43.2, lon_min=-3.9, lat_max=58.1, lon_max=20.3),
    "arome": BBox(lat_min=37.5, lon_min=-12.0, lat_max=55.4, lon_max=16.0),
}


def models_available_for_bbox(bbox: BBox) -> list[ModelPreset]:
    """Return presets whose coverage is global or fully contains the query bbox."""
    out: list[ModelPreset] = []
    for preset in MODEL_PRESETS:
        cov = MODEL_COVERAGE.get(preset.id)
        if cov is None:
            out.append(preset)
            continue
        if (
            cov.lat_min <= bbox.lat_min
            and cov.lon_min <= bbox.lon_min
            and cov.lat_max >= bbox.lat_max
            and cov.lon_max >= bbox.lon_max
        ):
            out.append(preset)
    return out

OPEN_METEO_MODEL_MAP = {
    "gfs": "gfs_seamless",
    "ecmwf": "ecmwf_ifs025",
    "icon": "icon_global",
    "ukmo": "ukmo_global_deterministic_10km",
    "gem": "gem_global",
    "icon_eu": "icon_eu",
    "icon_d2": "icon_d2",
    "arome": "meteofrance_arome_france_hd",
    "arpege": "meteofrance_arpege_world",
    "arpege_eu": "meteofrance_arpege_europe",
    "harmonie": "knmi_harmonie_arome_europe",
}

# --- Geo data sources -------------------------------------------------------

GSHHG_DIR = DATA_DIR / "geo" / "gshhg"
GSHHG_LAND_SHP = GSHHG_DIR / "GSHHS_h_L1.shp"
NATURAL_EARTH_LAND = DATA_DIR / "geo" / "ne_110m_land.geojson"

EMODNET_WCS_URL = os.environ.get(
    "SAILING_EMODNET_WCS",
    "https://ows.emodnet-bathymetry.eu/wcs",
)
GEBCO_WCS_URL = os.environ.get(
    "SAILING_GEBCO_WCS",
    "https://www.gebco.net/data_and_products/gebco_web_services/web_map_service/mapserv",
)
