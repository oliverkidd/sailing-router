"""Core data models (pydantic) — the shared contract for the whole system.

`apps/web/src/lib/types.ts` is generated from the FastAPI OpenAPI schema, so these
models are the single source of truth. Conventions pinned here:

- Positions are (lat, lon) in WGS84 degrees.
- All datetimes are UTC ISO-8601.
- Wind: TWD is the direction the wind blows *from* (meteorological). TWA is 0..180.
- Current: `set` is the direction the current flows *toward*, rate in knots.
- Speeds through/over water are in knots unless a field name says otherwise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# (lat, lon), WGS84 degrees. Kept as a plain tuple to match the plan's LatLon.
LatLon = tuple[float, float]

Maneuver = Literal["none", "tack", "gybe", "mark"]
ConnectorId = Literal["open-meteo", "dwd", "ecmwf-open", "meteofrance"]
RoundingSide = Literal["port", "starboard", "either"]


class Mark(BaseModel):
    """An ordered rounding mark on the course."""

    position: LatLon
    radius_nm: float = 0.1
    # Side is only enforced when != "either". Strict rounding geometry is TODO(v2).
    rounding: RoundingSide = "either"
    name: str | None = None


class BoatConfig(BaseModel):
    """Configurable boat model that drives the router."""

    polar_id: str
    tack_penalty_s: float = 30.0
    gybe_penalty_s: float = 20.0
    # Optional manual overrides; if None, derived from polar VMG.
    upwind_twa_deg: float | None = None  # target beat angle ("tack angle")
    downwind_twa_deg: float | None = None  # target run/gybe angle
    # Leeway (wind drift): leeway_deg = leeway_coeff * TWS / max(bsp, eps)^2, capped.
    leeway_coeff: float = 0.0  # 0 disables; typical ~ small positive
    leeway_max_deg: float = 8.0
    current_drift_factor: float = 1.0  # 1.0 = full current effect
    polar_efficiency: float = 1.0  # 0..1 real-world derate (sea state / night)


class Course(BaseModel):
    start: LatLon
    finish: LatLon
    marks: list[Mark] = Field(default_factory=list)  # ordered rounding marks
    exclusions: list[list[LatLon]] = Field(default_factory=list)  # polygons to avoid
    depart_utc: datetime
    min_depth_m: float = 3.0


class ModelPreset(BaseModel):
    id: str  # "ecmwf" | "icon" | "icon_eu" | "arpege" | "arome" | "ukmo"
    label: str
    resolution_deg: float
    connector: ConnectorId
    color: str


class RouteRequest(BaseModel):
    course: Course
    boat: BoatConfig
    model_ids: list[str]  # one route per model
    use_terrain_wind: bool = True  # WindNinja downscaling
    use_currents: bool = True
    time_step_s: int = 300
    heading_step_deg: float = 7.5


class RouteLeg(BaseModel):
    t_utc: datetime
    position: LatLon
    tws: float
    twd: float
    twa: float
    heading: float
    bsp: float  # boat speed through water
    sog: float  # over-ground after current + leeway
    cog: float
    cur_set_deg: float
    cur_rate_kn: float
    maneuver: Maneuver = "none"


class ModelRoute(BaseModel):
    model_id: str
    legs: list[RouteLeg]
    eta_utc: datetime
    elapsed_s: int
    distance_nm: float
    n_tacks: int
    n_gybes: int
    forecast_exhausted: bool


class RouteResponse(BaseModel):
    routes: list[ModelRoute]
    warnings: list[str] = Field(default_factory=list)


# ---- Boat / polar upload ---------------------------------------------------


class BoatResponse(BaseModel):
    boat: BoatConfig
    polar_id: str
    tws_axis: list[float]
    twa_axis: list[float]


# ---- Fetch endpoints -------------------------------------------------------


class BBox(BaseModel):
    """Bounding box in degrees. lat_min < lat_max, lon_min < lon_max."""

    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float

    def key(self) -> str:
        return f"{self.lat_min:.3f}_{self.lon_min:.3f}_{self.lat_max:.3f}_{self.lon_max:.3f}"


class ForecastFetchRequest(BaseModel):
    model_ids: list[str]
    bbox: BBox
    cycle: str | None = None  # e.g. "2026-07-10T00:00:00Z"; None => latest
    horizon_h: int = 72


class ForecastModelStatus(BaseModel):
    model_id: str
    status: Literal["cached", "fetched", "error"]
    cache_key: str | None = None
    n_times: int | None = None
    message: str | None = None


class ForecastFetchResponse(BaseModel):
    results: list[ForecastModelStatus]


class CurrentFetchRequest(BaseModel):
    bbox: BBox
    window_start_utc: datetime
    window_end_utc: datetime
    source: Literal["copernicus-smoc", "copernicus-med", "fes"] = "copernicus-smoc"


class CurrentFetchResponse(BaseModel):
    status: Literal["cached", "fetched", "error"]
    cache_key: str | None = None
    source: str
    message: str | None = None


class ElevationResponse(BaseModel):
    lat: float
    lon: float
    elevation_m: float | None
