/** Compact u/v grid from /wind/area (browse + playback share one series path). */

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

export interface WindSeries {
  lat0: number;
  lon0: number;
  dlat: number;
  dlon: number;
  nlat: number;
  nlon: number;
  times: string[];
  u: number[][];
  v: number[][];
  speed_kn: number[][];
  model_id?: string;
  bbox?: MapBounds;
  error?: string;
  message?: string;
  zoom_tier?: string;
  target_res_deg?: number;
  terrain_applied?: boolean;
  terrain_library?: "windninja" | "identity" | "none" | string;
  terrain_library_version?: string;
}

export interface WindSeriesResult {
  series: WindSeries | null;
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

/** Fetch multi-hour wind series for a viewport (GRIB-backed /wind/area). */
export async function fetchWindArea(
  bbox: MapBounds,
  modelId: string,
  startUtc: string,
  endUtc: string,
  signal?: AbortSignal,
  zoom?: number,
  terrain?: boolean,
): Promise<WindSeriesResult> {
  const extra: Record<string, string> = {
    model_id: modelId,
    start_utc: startUtc,
    end_utc: endUtc,
  };
  if (zoom != null && Number.isFinite(zoom)) {
    extra.zoom = String(zoom);
  }
  if (terrain === false) {
    extra.terrain = "false";
  }
  const q = bboxParams(bbox, extra);
  const t0 = performance.now();
  try {
    const res = await fetch(`${API_BASE}/wind/area?${q}`, { signal });
    const elapsedMs = Math.round(performance.now() - t0);
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      console.warn("[forecast] /wind/area HTTP error", {
        modelId,
        status: res.status,
        elapsedMs,
        startUtc,
        endUtc,
        body: body.slice(0, 200),
      });
      return { series: null, error: `Wind area API error (${res.status})` };
    }
    const data = (await res.json()) as WindSeries;
    if (data.error) {
      console.warn("[forecast] /wind/area unavailable", {
        modelId,
        error: data.error,
        message: data.message,
        elapsedMs,
      });
      return { series: null, error: data.message || "Wind unavailable" };
    }
    if (!data.times?.length) {
      console.warn("[forecast] /wind/area empty series", { modelId, elapsedMs });
      return { series: null, error: "Wind series empty" };
    }
    if (process.env.NODE_ENV === "development") {
      console.info("[forecast] /wind/area OK", {
        modelId,
        hours: data.times.length,
        elapsedMs,
        terrain: data.terrain_applied,
        tier: data.zoom_tier,
      });
    }
    return { series: data };
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      return { series: null, error: "aborted" };
    }
    console.warn("[forecast] /wind/area fetch failed", {
      modelId,
      elapsedMs: Math.round(performance.now() - t0),
      error: e instanceof Error ? e.message : String(e),
    });
    return {
      series: null,
      error: e instanceof Error ? e.message : "Wind fetch failed",
    };
  }
}

/** Cold-load wind via SSE stages (forcing → library → regrid → payload). */
export async function fetchWindAreaStream(
  bbox: MapBounds,
  modelId: string,
  startUtc: string,
  endUtc: string,
  onEvent: (event: import("./windLoadProgress").WindProgressEvent) => void,
  signal?: AbortSignal,
  zoom?: number,
  terrain?: boolean,
): Promise<WindSeriesResult> {
  const extra: Record<string, string> = {
    model_id: modelId,
    start_utc: startUtc,
    end_utc: endUtc,
  };
  if (zoom != null && Number.isFinite(zoom)) {
    extra.zoom = String(zoom);
  }
  if (terrain === false) {
    extra.terrain = "false";
  }
  const q = bboxParams(bbox, extra);
  const t0 = performance.now();
  try {
    const res = await fetch(`${API_BASE}/wind/area/stream?${q}`, {
      signal,
      headers: { Accept: "text/event-stream" },
    });
    if (!res.ok) {
      return { series: null, error: `Wind stream API error (${res.status})` };
    }
    if (!res.body) {
      return { series: null, error: "Wind stream returned no body" };
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let series: WindSeries | null = null;
    let streamError: string | null = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const dataLine = part
          .split("\n")
          .map((l) => l.trimEnd())
          .find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        const raw = dataLine.slice(5).trim();
        if (!raw || raw === "[DONE]") continue;
        let event: import("./windLoadProgress").WindProgressEvent;
        try {
          event = JSON.parse(raw) as import("./windLoadProgress").WindProgressEvent;
        } catch {
          continue;
        }
        onEvent(event);
        if (event.type === "result") {
          series = event.series as unknown as WindSeries;
        } else if (event.type === "error" && !series) {
          streamError = event.message;
        }
      }
    }

    const elapsedMs = Math.round(performance.now() - t0);
    if (series?.times?.length) {
      if (process.env.NODE_ENV === "development") {
        console.info("[forecast] /wind/area/stream OK", {
          modelId,
          hours: series.times.length,
          elapsedMs,
          terrain: series.terrain_applied,
          tier: series.zoom_tier,
        });
      }
      return { series };
    }
    if (series?.error) {
      return { series: null, error: series.message || "Wind unavailable" };
    }
    return { series: null, error: streamError || "Wind stream ended without a result" };
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      return { series: null, error: "aborted" };
    }
    return {
      series: null,
      error: e instanceof Error ? e.message : "Wind stream failed",
    };
  }
}

/** @deprecated Use fetchWindArea */
export const fetchWindSeries = fetchWindArea;

/** Merge two WindSeries on the same grid (append/extend lazy days). */
export function mergeSeries(a: WindSeries, b: WindSeries): WindSeries {
  const eps = 1e-6;
  const sameGrid =
    a.nlat === b.nlat &&
    a.nlon === b.nlon &&
    Math.abs(a.lat0 - b.lat0) < eps &&
    Math.abs(a.lon0 - b.lon0) < eps &&
    Math.abs(a.dlat - b.dlat) < eps &&
    Math.abs(a.dlon - b.dlon) < eps;
  if (!sameGrid) return b;

  const byTime = new Map<string, { u: number[]; v: number[]; speed_kn: number[] }>();
  for (let i = 0; i < a.times.length; i++) {
    byTime.set(a.times[i], {
      u: a.u[i] ?? [],
      v: a.v[i] ?? [],
      speed_kn: a.speed_kn[i] ?? [],
    });
  }
  for (let i = 0; i < b.times.length; i++) {
    byTime.set(b.times[i], {
      u: b.u[i] ?? [],
      v: b.v[i] ?? [],
      speed_kn: b.speed_kn[i] ?? [],
    });
  }
  const times = Array.from(byTime.keys()).sort(
    (x, y) => new Date(x).getTime() - new Date(y).getTime(),
  );
  return {
    lat0: a.lat0,
    lon0: a.lon0,
    dlat: a.dlat,
    dlon: a.dlon,
    nlat: a.nlat,
    nlon: a.nlon,
    times,
    u: times.map((t) => byTime.get(t)!.u),
    v: times.map((t) => byTime.get(t)!.v),
    speed_kn: times.map((t) => byTime.get(t)!.speed_kn),
    model_id: b.model_id ?? a.model_id,
    bbox: b.bbox ?? a.bbox,
  };
}

const HOUR_MS = 3600 * 1000;

export function floorHourIso(iso: string): string {
  const d = new Date(iso);
  d.setUTCMinutes(0, 0, 0);
  return d.toISOString();
}

export function hoursAheadIso(fromIso: string, hours: number): string {
  const d = new Date(fromIso);
  d.setUTCHours(d.getUTCHours() + hours);
  return d.toISOString();
}

/** True when the series already has a frame for this UTC hour (±1 min). */
export function seriesCoversHour(series: WindSeries, hourUtc: string): boolean {
  const t = new Date(hourUtc).getTime();
  return series.times.some((x) => Math.abs(new Date(x).getTime() - t) < 60_000);
}

/** Last hour of the contiguous forward run that includes focus (null if focus missing). */
export function contiguousRunMax(series: WindSeries, focusHourUtc: string): string | null {
  const focus = floorHourIso(focusHourUtc);
  if (!seriesCoversHour(series, focus)) return null;
  let cur = focus;
  for (;;) {
    const next = hoursAheadIso(cur, 1);
    if (!seriesCoversHour(series, next)) return cur;
    cur = next;
  }
}

/** First contiguous missing sub-range inside [start, end] inclusive; null if fully covered. */
export function missingRange(
  series: WindSeries | null | undefined,
  startUtc: string,
  endUtc: string,
): { start: string; end: string } | null {
  const start = floorHourIso(startUtc);
  const end = floorHourIso(endUtc);
  if (new Date(end).getTime() < new Date(start).getTime()) return null;

  let rangeStart: string | null = null;
  let rangeEnd: string | null = null;
  let cur = start;
  while (new Date(cur).getTime() <= new Date(end).getTime()) {
    const covered = !!series && seriesCoversHour(series, cur);
    if (!covered) {
      if (!rangeStart) rangeStart = cur;
      rangeEnd = cur;
    } else if (rangeStart) {
      break;
    }
    cur = hoursAheadIso(cur, 1);
  }
  if (!rangeStart || !rangeEnd) return null;
  return { start: rangeStart, end: rangeEnd };
}

/** Linear-in-time slice of a WindSeries into a single MeteoGrid. */
export function sliceSeries(series: WindSeries, tUtc: string): MeteoGrid {
  const tMs = new Date(tUtc).getTime();
  const timesMs = series.times.map((t) => new Date(t).getTime());
  const n = timesMs.length;

  let k0 = 0;
  if (tMs <= timesMs[0]) {
    k0 = 0;
  } else if (tMs >= timesMs[n - 1]) {
    k0 = n - 1;
  } else {
    k0 = Math.max(0, timesMs.findIndex((tm) => tm > tMs) - 1);
  }
  const k1 = Math.min(n - 1, k0 + 1);

  const t0 = timesMs[k0];
  const t1 = timesMs[k1];
  // Sparse on-demand hours: never interpolate across a multi-hour gap.
  const ft =
    k0 === k1 || t1 === t0 || t1 - t0 > HOUR_MS + 1000
      ? tMs - t0 <= t1 - tMs
        ? 0
        : 1
      : (tMs - t0) / (t1 - t0);

  const u0 = series.u[k0] ?? [];
  const v0 = series.v[k0] ?? [];
  const u1 = series.u[k1] ?? u0;
  const v1 = series.v[k1] ?? v0;
  const len = Math.min(u0.length, v0.length, u1.length || u0.length, v1.length || v0.length);

  const u: number[] = new Array(len);
  const v: number[] = new Array(len);
  const speed_kn: number[] = new Array(len);
  for (let i = 0; i < len; i++) {
    const ui = (u0[i] ?? 0) * (1 - ft) + (u1[i] ?? 0) * ft;
    const vi = (v0[i] ?? 0) * (1 - ft) + (v1[i] ?? 0) * ft;
    u[i] = ui;
    v[i] = vi;
    speed_kn[i] = Math.hypot(ui, vi) * 1.943844;
  }

  return {
    lat0: series.lat0,
    lon0: series.lon0,
    dlat: series.dlat,
    dlon: series.dlon,
    nlat: series.nlat,
    nlon: series.nlon,
    u,
    v,
    speed_kn,
    t_utc: tUtc,
    model_id: series.model_id,
    bbox: series.bbox,
  };
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
