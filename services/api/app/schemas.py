"""Core data models (pydantic) — the shared contract for the whole system.

`apps/web/src/lib/types.ts` mirrors these models. Conventions:

- Positions are (lat, lon) in WGS84 degrees.
- All datetimes are UTC ISO-8601.
- Wind: TWD is the direction the wind blows *from* (meteorological). TWA is 0..180.
- Speeds through/over water are in knots unless a field name says otherwise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LatLon = tuple[float, float]

Maneuver = Literal["none", "tack", "gybe", "mark"]
ConnectorId = Literal[
    "ecmwf-ifs",
    "ecmwf-aifs",
    "dwd-icon",
    "noaa-gfs",
    "eccc-gem",
    "mf-arome",
    "mf-arpege",
    "knmi-harmonie",
]
RoundingSide = Literal["port", "starboard", "either"]


class Mark(BaseModel):
    """An ordered rounding mark on the course."""

    position: LatLon
    radius_nm: float = 0.1
    rounding: RoundingSide = "either"
    name: str | None = None


class BoatConfig(BaseModel):
    """Configurable boat model that drives the router."""

    polar_id: str
    tack_penalty_s: float = 30.0
    gybe_penalty_s: float = 20.0
    upwind_twa_deg: float | None = None
    downwind_twa_deg: float | None = None
    leeway_coeff: float = 0.0
    leeway_max_deg: float = 8.0
    polar_efficiency: float = 1.0


class Course(BaseModel):
    start: LatLon
    finish: LatLon
    marks: list[Mark] = Field(default_factory=list)
    exclusions: list[list[LatLon]] = Field(default_factory=list)
    depart_utc: datetime
    min_depth_m: float = 3.0


class ModelPreset(BaseModel):
    id: str
    label: str
    resolution_deg: float
    connector: ConnectorId
    color: str


class RouteRequest(BaseModel):
    course: Course
    boat: BoatConfig
    model_ids: list[str]
    time_step_s: int = 300
    heading_step_deg: float = 7.5


class RouteLeg(BaseModel):
    t_utc: datetime
    position: LatLon
    tws: float
    twd: float
    twa: float
    heading: float
    bsp: float
    sog: float
    cog: float
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


class BoatResponse(BaseModel):
    boat: BoatConfig
    polar_id: str
    tws_axis: list[float]
    twa_axis: list[float]


class BBox(BaseModel):
    """Bounding box in degrees. lat_min < lat_max, lon_min < lon_max."""

    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float

    def key(self) -> str:
        return f"{self.lat_min:.3f}_{self.lon_min:.3f}_{self.lat_max:.3f}_{self.lon_max:.3f}"


class ElevationResponse(BaseModel):
    lat: float
    lon: float
    elevation_m: float | None
    depth_m: float | None = None
    on_land: bool | None = None


class CoursePrepareRequest(BaseModel):
    course: Course
    model_ids: list[str]


class CoursePrepareModelStatus(BaseModel):
    model_id: str
    status: Literal["cached", "fetched", "error", "skipped"]
    n_times: int | None = None
    message: str | None = None


class CoursePrepareResponse(BaseModel):
    bbox: BBox
    horizon_h: int
    geo: Literal["cached", "fetched", "error"]
    models: list[CoursePrepareModelStatus]
