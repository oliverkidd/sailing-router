"""Core data models (pydantic) — the shared contract for the whole system.

`apps/web/src/lib/types.ts` mirrors these models. Conventions:

- Positions are (lat, lon) in WGS84 degrees.
- All datetimes are UTC ISO-8601.
- Wind: TWD is the direction the wind blows *from* (meteorological). TWA is 0..180.
- Speeds through/over water are in knots unless a field name says otherwise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class RouterParamsOverride(BaseModel):
    """Partial RouterParams — omitted fields keep engine defaults."""

    model_config = ConfigDict(extra="forbid")

    time_step_s: int | None = Field(None, ge=10, le=3600)
    heading_step_deg: float | None = Field(None, gt=0, le=45)
    sector_width_deg: float | None = Field(None, gt=0, le=45)
    cluster_cell_deg: float | None = Field(None, gt=0, le=1)
    max_frontier: int | None = Field(None, ge=10, le=20000)
    max_steps_per_leg: int | None = Field(None, ge=10, le=20000)
    maneuver_prune_nm: float | None = Field(None, ge=0, le=10)
    depth_safety_margin_m: float | None = Field(None, ge=0, le=20)
    no_progress_steps: int | None = Field(None, ge=1, le=2000)
    max_consecutive_maneuvers: int | None = Field(None, ge=0, le=200)
    maneuver_overflow_nm: float | None = Field(None, ge=0, le=20)
    min_bsp_fraction_of_beat: float | None = Field(None, ge=0, le=1)
    max_commit_s: float | None = Field(None, ge=10, le=14400)
    wind_delta_twd_deg: float | None = Field(None, ge=0, le=180)
    wind_delta_tws_kn: float | None = Field(None, ge=0, le=30)
    mark_probe_nm: float | None = Field(None, ge=0.05, le=10)
    near_mark_nm: float | None = Field(None, ge=0.05, le=10)
    fan_half_max_deg: float | None = Field(None, ge=5, le=180)
    fan_probe_nm_max: float | None = Field(None, ge=0.05, le=5)
    fan_probe_nm_min: float | None = Field(None, ge=0.02, le=5)
    fan_probe_mark_frac: float | None = Field(None, ge=0.05, le=1)
    same_heading_bonus_s: float | None = Field(None, ge=0, le=60)
    same_heading_band_deg: float | None = Field(None, ge=0, le=45)
    vmg_hysteresis_deg: float | None = Field(None, ge=0, le=90)
    course_change_free_deg: float | None = Field(None, ge=0, le=90)
    course_change_moderate_deg: float | None = Field(None, ge=0, le=90)
    course_change_tws_scale_kn: float | None = Field(None, ge=1, le=50)
    course_change_heavy_frac_small: float | None = Field(None, ge=0, le=1)
    course_change_heavy_frac_large: float | None = Field(None, ge=0, le=1)
    course_change_mild_frac: float | None = Field(None, ge=0, le=1)
    downwind_twa_gate_deg: float | None = Field(None, ge=0, le=180)
    heading_ref_max_err_deg: float | None = Field(None, ge=0, le=180)
    opposite_maneuver_course_err_deg: float | None = Field(None, ge=0, le=180)
    opposite_tack_twa_slack_deg: float | None = Field(None, ge=0, le=45)
    opposite_gybe_twa_slack_deg: float | None = Field(None, ge=0, le=45)
    opposite_maneuver_heading_spread_deg: float | None = Field(None, ge=0, le=180)
    rethink_heading_vs_bearing_deg: float | None = Field(None, ge=0, le=90)
    layline_stop_deg: float | None = Field(None, ge=0, le=180)
    past_layline_deg: float | None = Field(None, ge=0, le=180)
    light_air_twd_tws_kn: float | None = Field(None, ge=0, le=20)
    prune_floor_bsp_kn: float | None = Field(None, ge=0.05, le=10)
    soft_cost_s_per_nm: float | None = Field(None, ge=0, le=3600)
    wind_epoch_commit_frac: float | None = Field(None, ge=0, le=2)
    commit_arrival_buffer_s: float | None = Field(None, ge=0, le=600)
    stall_rem_factor: float | None = Field(None, ge=0, le=20)
    stall_rem_offset: int | None = Field(None, ge=0, le=500)
    geo_hit_retry_fraction: float | None = Field(None, ge=0, le=1)
    short_commit_min_s: float | None = Field(None, ge=5, le=600)
    creep_commit_min_s: float | None = Field(None, ge=5, le=600)
    finish_snap_radius_nm: float | None = Field(None, ge=0.02, le=2)
    mark_arrival_break_nm: float | None = Field(None, ge=0.01, le=1)
    waypoint_guidance: int | None = Field(None, ge=0, le=1)
    waypoint_clearance_nm: float | None = Field(None, ge=0, le=5)
    waypoint_corridor_margin_nm: float | None = Field(None, ge=0.5, le=30)
    waypoint_max_points: int | None = Field(None, ge=1, le=32)
    waypoint_grid_stride: int | None = Field(None, ge=1, le=16)
    waypoint_pass_nm: float | None = Field(None, ge=0, le=5)


class RouteRequest(BaseModel):
    course: Course
    boat: BoatConfig
    model_ids: list[str]
    time_step_s: int = 300
    heading_step_deg: float = 7.5
    router_params: RouterParamsOverride | None = None


class TuningField(BaseModel):
    name: str
    category: str
    label: str
    description: str
    default: float | int | None
    min: float | None = None
    max: float | None = None
    step: float = 1
    group: Literal["router", "boat", "course", "request"]
    nullable: bool = False


class TuningSchemaResponse(BaseModel):
    categories: list[str]
    fields: list[TuningField]
    router_defaults: dict[str, Any]


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
    depth_m: float | None = None
    hazard_cost: float | None = None


class ModelRoute(BaseModel):
    model_id: str
    legs: list[RouteLeg]
    eta_utc: datetime
    elapsed_s: int
    distance_nm: float
    n_tacks: int
    n_gybes: int
    forecast_exhausted: bool
    unreachable: bool = False
    warnings: list[str] = Field(default_factory=list)
    data_source: dict[str, str] = Field(default_factory=dict)
    # Advisory headland chain the router steered by, when waypoint guidance ran.
    waypoints: list[LatLon] | None = None


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
    # When mosaic has nodata over water (common shoreward of the 2 m isobath),
    # depth is only known as shallower than this chart contour.
    depth_lt_m: float | None = None
    on_land: bool | None = None
    # True when a loaded DEM GeoTIFF footprint contains this point (may still
    # return null elevation over ocean nodata inside the mosaic).
    dem_available: bool = False


class DemFootprint(BaseModel):
    name: str
    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float


class DemCoverageResponse(BaseModel):
    footprints: list[DemFootprint]


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
