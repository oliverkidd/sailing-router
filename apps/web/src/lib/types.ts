// Shared types mirroring the FastAPI pydantic schemas (services/api/app/schemas.py).
// TODO(v2): auto-generate from the OpenAPI schema via packages/shared-schemas.

export type LatLon = [number, number]; // [lat, lon] WGS84 degrees
export type Maneuver = "none" | "tack" | "gybe" | "mark";
export type ConnectorId = "open-meteo" | "dwd" | "ecmwf-open" | "meteofrance";
export type RoundingSide = "port" | "starboard" | "either";

export interface Mark {
  position: LatLon;
  radius_nm: number;
  rounding: RoundingSide;
  name?: string | null;
}

export interface BoatConfig {
  polar_id: string;
  tack_penalty_s: number;
  gybe_penalty_s: number;
  upwind_twa_deg?: number | null;
  downwind_twa_deg?: number | null;
  leeway_coeff: number;
  leeway_max_deg: number;
  current_drift_factor: number;
  polar_efficiency: number;
}

export interface Course {
  start: LatLon;
  finish: LatLon;
  marks: Mark[];
  exclusions: LatLon[][];
  depart_utc: string;
  min_depth_m: number;
}

export interface ModelPreset {
  id: string;
  label: string;
  resolution_deg: number;
  connector: ConnectorId;
  color: string;
}

export interface RouteRequest {
  course: Course;
  boat: BoatConfig;
  model_ids: string[];
  use_terrain_wind: boolean;
  use_currents: boolean;
  time_step_s: number;
  heading_step_deg: number;
}

export interface RouteLeg {
  t_utc: string;
  position: LatLon;
  tws: number;
  twd: number;
  twa: number;
  heading: number;
  bsp: number;
  sog: number;
  cog: number;
  cur_set_deg: number;
  cur_rate_kn: number;
  maneuver: Maneuver;
}

export interface ModelRoute {
  model_id: string;
  legs: RouteLeg[];
  eta_utc: string;
  elapsed_s: number;
  distance_nm: number;
  n_tacks: number;
  n_gybes: number;
  forecast_exhausted: boolean;
}

export interface RouteResponse {
  routes: ModelRoute[];
  warnings: string[];
}
