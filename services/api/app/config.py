"""Configuration: GRIB model presets, coverage, and runtime paths."""

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

# --- Forecast model presets (GRIB-only) -------------------------------------

MODEL_PRESETS: list[ModelPreset] = [
    ModelPreset(
        id="ecmwf",
        label="ECMWF IFS",
        resolution_deg=0.25,
        connector="ecmwf-ifs",
        color="#2dd4bf",
    ),
    ModelPreset(
        id="aifs",
        label="ECMWF AIFS",
        resolution_deg=0.25,
        connector="ecmwf-aifs",
        color="#14b8a6",
    ),
    ModelPreset(
        id="gfs",
        label="GFS",
        resolution_deg=0.25,
        connector="noaa-gfs",
        color="#ef4444",
    ),
    ModelPreset(
        id="gem",
        label="GEM GDPS",
        resolution_deg=0.15,
        connector="eccc-gem",
        color="#a78bfa",
    ),
    ModelPreset(
        id="icon_eu",
        label="ICON-EU",
        resolution_deg=0.0625,
        connector="dwd-icon",
        color="#fbbf24",
    ),
    ModelPreset(
        id="icon_d2",
        label="ICON-D2",
        resolution_deg=0.02,
        connector="dwd-icon",
        color="#fcd34d",
    ),
    ModelPreset(
        id="arome",
        label="AROME France HD",
        resolution_deg=0.025,
        connector="mf-arome",
        color="#f472b6",
    ),
    ModelPreset(
        id="arpege_eu",
        label="ARPEGE EU",
        resolution_deg=0.1,
        connector="mf-arpege",
        color="#fda4af",
    ),
    ModelPreset(
        id="harmonie",
        label="Harmonie (KNMI)",
        resolution_deg=0.02,
        connector="knmi-harmonie",
        color="#38bdf8",
    ),
]

MODEL_PRESETS_BY_ID = {m.id: m for m in MODEL_PRESETS}

MODEL_GROUPS: dict[str, list[str]] = {
    "global": ["ecmwf", "aifs", "gfs", "gem"],
    "regional": ["icon_eu", "icon_d2", "arome", "arpege_eu", "harmonie"],
}

# Geographic coverage. None = global.
MODEL_COVERAGE: dict[str, BBox | None] = {
    "ecmwf": None,
    "aifs": None,
    "gfs": None,
    "gem": None,
    "icon_eu": BBox(lat_min=29.5, lon_min=-23.5, lat_max=70.5, lon_max=45.0),
    "icon_d2": BBox(lat_min=43.2, lon_min=-3.9, lat_max=58.1, lon_max=20.3),
    "arome": BBox(lat_min=37.5, lon_min=-12.0, lat_max=55.4, lon_max=16.0),
    "arpege_eu": BBox(lat_min=20.0, lon_min=-32.0, lat_max=72.0, lon_max=42.0),
    "harmonie": BBox(lat_min=39.0, lon_min=-25.0, lat_max=62.0, lon_max=30.0),
}

# Env var required for key-gated models.
KEY_ENV: dict[str, str] = {
    "arome": "METEOFRANCE_API_KEY",
    "arpege_eu": "METEOFRANCE_API_KEY",
    "harmonie": "KNMI_API_KEY",
}

WIND_TTL_S = 3 * 3600


def model_is_configured(model_id: str) -> bool:
    """True if the model needs no key, or its API key env var is set."""
    env_name = KEY_ENV.get(model_id)
    if env_name is None:
        return True
    return bool(os.environ.get(env_name, "").strip())


def models_available_for_bbox(bbox: BBox) -> list[ModelPreset]:
    """Presets whose coverage contains bbox AND that are configured (key present)."""
    out: list[ModelPreset] = []
    for preset in MODEL_PRESETS:
        if not model_is_configured(preset.id):
            continue
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


# --- Geo data sources -------------------------------------------------------

GSHHG_DIR = DATA_DIR / "geo" / "gshhg"
GSHHG_LAND_SHP = GSHHG_DIR / "GSHHS_h_L1.shp"
NATURAL_EARTH_LAND = DATA_DIR / "geo" / "ne_110m_land.geojson"
NATURAL_EARTH_PLACES = DATA_DIR / "geo" / "ne_10m_populated_places.geojson"
NATURAL_EARTH_GEOGRAPHY = DATA_DIR / "geo" / "ne_10m_geography_regions_points.geojson"
NATURAL_EARTH_GEOGRAPHY_POLYS = DATA_DIR / "geo" / "ne_10m_geography_regions_polys.geojson"

EMODNET_WCS_URL = os.environ.get(
    "SAILING_EMODNET_WCS",
    "https://ows.emodnet-bathymetry.eu/wcs",
)
GEBCO_WCS_URL = os.environ.get(
    "SAILING_GEBCO_WCS",
    "https://www.gebco.net/data_and_products/gebco_web_services/web_map_service/mapserv",
)
