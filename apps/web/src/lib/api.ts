// Thin typed client for the FastAPI backend. The frontend only ever exchanges
// JSON — it never parses GRIB/NetCDF or runs WindNinja.

import type {
  BoatConfig,
  Course,
  ModelPreset,
  PolarCurve,
  RouteRequest,
  RouteResponse,
} from "./types";

export interface CourseSummary {
  id: string;
  name: string;
  region?: string | null;
}

export interface BoatResponse {
  boat: BoatConfig;
  polar_id: string;
  tws_axis: number[];
  twa_axis: number[];
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

export const api = {
  health: () => req<{ status: string; version: string }>("/health"),
  models: () => req<ModelPreset[]>("/models"),
  route: (body: RouteRequest) =>
    req<RouteResponse>("/route", { method: "POST", body: JSON.stringify(body) }),
  elevation: (lat: number, lon: number) =>
    req<{ lat: number; lon: number; elevation_m: number | null }>(
      `/elevation?lat=${lat}&lon=${lon}`,
    ),
  courses: () => req<CourseSummary[]>("/courses"),
  course: (id: string) => req<Course & { name?: string }>(`/courses/${id}`),
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
};

export { API_BASE };
