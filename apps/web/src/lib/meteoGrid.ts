/** Compact u/v grid from /wind/grid tiles. */

import { API_BASE } from "./api";
import type { MapBounds } from "./mapOverlays";

export interface MeteoGrid {
  lat0: number;
  lon0: number;
  dlat: number;
  dlon: number;
  nlat: number;
  nlon: number;
  u: number[];
  v: number[];
  speed_kn: number[];
  t_utc: string;
  model_id?: string;
  source?: string;
  bbox?: MapBounds;
  error?: string;
  message?: string;
}

export interface MeteoGridResult {
  grid: MeteoGrid | null;
  error?: string;
}

function bboxParams(b: MapBounds, extra?: Record<string, string>) {
  return new URLSearchParams({
    lat_min: String(b.lat_min),
    lon_min: String(b.lon_min),
    lat_max: String(b.lat_max),
    lon_max: String(b.lon_max),
    ...extra,
  });
}

/** Fetch one LOD wind tile at explicit dims (no zoom param). */
export async function fetchWindTile(
  bbox: MapBounds,
  tUtc: string,
  modelId: string,
  nx: number,
  ny: number,
  signal?: AbortSignal,
): Promise<MeteoGridResult> {
  const q = bboxParams(bbox, {
    t_utc: tUtc,
    model_id: modelId,
    nx: String(nx),
    ny: String(ny),
  });
  const res = await fetch(`${API_BASE}/wind/grid?${q}`, { signal });
  if (!res.ok) {
    return { grid: null, error: `Wind API error (${res.status})` };
  }
  const data = (await res.json()) as MeteoGrid;
  if (data.error) {
    return { grid: null, error: data.message || "Wind forecast unavailable" };
  }
  return { grid: data };
}

/** Bilinear sample u/v at lat/lon from a regular grid. */
export function sampleGrid(
  grid: MeteoGrid,
  lat: number,
  lon: number,
): { u: number; v: number; speed: number } {
  const { lat0, lon0, dlat, dlon, nlat, nlon, u, v, speed_kn } = grid;
  if (nlat < 2 || nlon < 2) {
    return { u: u[0] ?? 0, v: v[0] ?? 0, speed: speed_kn[0] ?? 0 };
  }
  const fi = Math.max(0, Math.min(nlat - 1.001, (lat - lat0) / dlat));
  const fj = Math.max(0, Math.min(nlon - 1.001, (lon - lon0) / dlon));
  const i0 = Math.floor(fi);
  const j0 = Math.floor(fj);
  const i1 = Math.min(i0 + 1, nlat - 1);
  const j1 = Math.min(j0 + 1, nlon - 1);
  const fy = fi - i0;
  const fx = fj - j0;
  const idx = (ii: number, jj: number) => ii * nlon + jj;
  const u00 = u[idx(i0, j0)] ?? 0;
  const u01 = u[idx(i0, j1)] ?? 0;
  const u10 = u[idx(i1, j0)] ?? 0;
  const u11 = u[idx(i1, j1)] ?? 0;
  const v00 = v[idx(i0, j0)] ?? 0;
  const v01 = v[idx(i0, j1)] ?? 0;
  const v10 = v[idx(i1, j0)] ?? 0;
  const v11 = v[idx(i1, j1)] ?? 0;
  const su = (1 - fy) * ((1 - fx) * u00 + fx * u01) + fy * ((1 - fx) * u10 + fx * u11);
  const sv = (1 - fy) * ((1 - fx) * v00 + fx * v01) + fy * ((1 - fx) * v10 + fx * v11);
  const spd = Math.hypot(su, sv) * 1.943844;
  return { u: su, v: sv, speed: spd };
}
