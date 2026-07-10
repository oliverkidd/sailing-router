// Thin typed client for the FastAPI backend. The frontend only ever exchanges
// JSON — it never parses GRIB/NetCDF or runs WindNinja.

import type { ModelPreset, RouteRequest, RouteResponse } from "./types";

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
};

export { API_BASE };
