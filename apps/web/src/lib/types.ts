// Shared types mirroring the FastAPI pydantic schemas (services/api/app/schemas.py).

export type LatLon = [number, number]; // [lat, lon] WGS84 degrees
export type Maneuver = "none" | "tack" | "gybe" | "mark";
export type ConnectorId =
  | "ecmwf-ifs"
  | "ecmwf-aifs"
  | "dwd-icon"
  | "noaa-gfs"
  | "eccc-gem"
  | "mf-arome"
  | "mf-arpege"
  | "knmi-harmonie";
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
  maneuver: Maneuver;
  depth_m?: number | null;
  hazard_cost?: number | null;
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
  unreachable?: boolean;
  warnings?: string[];
  data_source?: Record<string, string>;
}

export interface RouteResponse {
  routes: ModelRoute[];
  warnings: string[];
}

/** SSE events from POST /route/stream */
export type RouteProgressEvent =
  | {
      type: "stage";
      key: "geo" | "wind" | "wind_extend" | string;
      status: "start" | "done" | "error";
      model_id?: string;
      ms?: number;
      n_times?: number;
      extend_h?: number;
    }
  | {
      type: "leg";
      model_id: string;
      index: number;
      total: number;
      name: string;
      status: "start" | "done" | "error";
      ms?: number;
      termination?: string;
      steps?: number;
      wind_at_calls?: number;
      blocked_calls?: number;
      expansions?: number;
    }
  | { type: "warning"; message: string }
  | { type: "error"; message: string }
  | { type: "result"; routes: ModelRoute[]; warnings: string[] };

export type RouteStepStatus = "pending" | "active" | "done" | "error";

export interface RouteProgressStep {
  id: string;
  label: string;
  status: RouteStepStatus;
  ms?: number;
  detail?: string;
}

export interface PolarCurve {
  polar_id: string;
  tws_axis: number[];
  twa_axis: number[];
  speed: number[][];
  vmg_targets: Record<string, { upwind: number; downwind: number }>;
}
