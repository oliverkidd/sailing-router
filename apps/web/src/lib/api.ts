// Thin typed client for the FastAPI backend.

import type {
  BoatConfig,
  Course,
  ModelPreset,
  PolarCurve,
  RouteRequest,
  RouteResponse,
} from "./types";
import type { MapBounds } from "./mapOverlays";

export interface BoatResponse {
  boat: BoatConfig;
  polar_id: string;
  tws_axis: number[];
  twa_axis: number[];
}

export interface CoursePrepareRequest {
  course: Course;
  model_ids: string[];
}

export interface CoursePrepareResponse {
  bbox: { lat_min: number; lon_min: number; lat_max: number; lon_max: number };
  horizon_h: number;
  geo: "cached" | "fetched" | "error";
  models: Array<{
    model_id: string;
    status: string;
    n_times?: number | null;
    message?: string | null;
  }>;
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${path} failed (${res.status}): ${body}`);
  }
  return res.json() as Promise<T>;
}

export interface ElevationResponse {
  lat: number;
  lon: number;
  elevation_m: number | null;
  depth_m?: number | null;
  /** Present when depth is only known as shallower than the chart's first isobath. */
  depth_lt_m?: number | null;
  on_land?: boolean | null;
}

export const api = {
  health: () => req<{ status: string; version: string }>("/health"),
  modelsAvailable: (bbox: MapBounds) => {
    const q = new URLSearchParams({
      lat_min: String(bbox.lat_min),
      lon_min: String(bbox.lon_min),
      lat_max: String(bbox.lat_max),
      lon_max: String(bbox.lon_max),
    });
    return req<ModelPreset[]>(`/models/available?${q}`);
  },
  windArea: (bbox: MapBounds, modelId: string, startUtc: string, endUtc: string) => {
    const q = new URLSearchParams({
      lat_min: String(bbox.lat_min),
      lon_min: String(bbox.lon_min),
      lat_max: String(bbox.lat_max),
      lon_max: String(bbox.lon_max),
      model_id: modelId,
      start_utc: startUtc,
      end_utc: endUtc,
    });
    return req<Record<string, unknown>>(`/wind/area?${q}`);
  },
  route: (body: RouteRequest) =>
    req<RouteResponse>("/route", { method: "POST", body: JSON.stringify(body) }),
  prepareCourse: (body: CoursePrepareRequest) =>
    req<CoursePrepareResponse>("/course/prepare", { method: "POST", body: JSON.stringify(body) }),
  sampleBoat: () => req<BoatResponse>("/boat/sample"),
  polarCurve: (polarId: string) => req<PolarCurve>(`/boat/polar/${polarId}`),
  uploadBoat: async (file: File, config: Partial<BoatConfig>): Promise<BoatResponse> => {
    const form = new FormData();
    form.append("file", file);
    form.append("config", JSON.stringify(config));
    const res = await fetch(`${API_BASE}/boat`, { method: "POST", body: form });
    if (!res.ok) throw new Error(`Upload failed (${res.status}): ${await res.text()}`);
    return res.json() as Promise<BoatResponse>;
  },
  elevation: (lat: number, lon: number) =>
    req<ElevationResponse>(`/elevation?lat=${lat.toFixed(5)}&lon=${lon.toFixed(5)}`),
};

export { API_BASE };
