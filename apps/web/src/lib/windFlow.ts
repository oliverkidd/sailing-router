/** Shared wind-flow integrator (u/v m/s on the same MeteoGrid as heatmap). */

import type { MeteoGrid } from "./meteoGrid";
import { sampleGrid } from "./meteoGrid";

const EARTH_CIRCUMFERENCE_M = 40075016.686;
const METERS_PER_DEG_LAT = 111320;

export const MIN_SPEED_KN = 0.5;

export function smoothstep(edge0: number, edge1: number, x: number): number {
  if (edge1 <= edge0) return x >= edge1 ? 1 : 0;
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

export type FlowStep = {
  lat: number;
  lon: number;
  u: number;
  v: number;
  speed: number;
};

export function metersPerPixel(lat: number, zoom: number): number {
  const cosLat = Math.max(0.15, Math.cos((lat * Math.PI) / 180));
  return (EARTH_CIRCUMFERENCE_M * cosLat) / (512 * 2 ** zoom);
}

/** Target on-screen gap between flow traces (px). Independent of camera zoom. */
export const FLOW_SPACING_PX = 72;
export const FLOW_MIN_PARTICLES = 120;
export const FLOW_MAX_PARTICLES = 320;
/** Terrain draping is the expensive path; keep the GeoJSON budget a bit lower. */
export const FLOW_MAX_PARTICLES_PITCHED = 250;

export type ScreenParticleLayout = {
  cols: number;
  rows: number;
  count: number;
};

/**
 * Screen-constant particle lattice. Count tracks canvas area / spacing, then
 * clamps; cols×rows is always exactly `count` so each slot owns one cell.
 */
export function screenParticleLayout(
  widthPx: number,
  heightPx: number,
  spacingPx: number,
  minCount: number,
  maxCount: number,
): ScreenParticleLayout {
  const w = Math.max(1, widthPx);
  const h = Math.max(1, heightPx);
  const spacing = Math.max(1, spacingPx);
  const min = Math.max(1, Math.floor(minCount));
  const max = Math.max(min, Math.floor(maxCount));

  let cols = Math.max(1, Math.round(w / spacing));
  let rows = Math.max(1, Math.round(h / spacing));

  const denserDim = (): "cols" | "rows" => (cols / w >= rows / h ? "cols" : "rows");

  while (cols * rows > max && (cols > 1 || rows > 1)) {
    if (denserDim() === "cols" && cols > 1) cols -= 1;
    else if (rows > 1) rows -= 1;
    else cols -= 1;
  }
  while (cols * rows < min) {
    if (denserDim() === "cols") rows += 1;
    else cols += 1;
  }

  return { cols, rows, count: cols * rows };
}

/** Clamped particle count for a canvas; same inputs as `screenParticleLayout`. */
export function screenParticleCount(
  widthPx: number,
  heightPx: number,
  spacingPx: number,
  cap: number,
  minCount = FLOW_MIN_PARTICLES,
): number {
  return screenParticleLayout(widthPx, heightPx, spacingPx, minCount, cap).count;
}

export function gridRect(grid: MeteoGrid): {
  latMin: number;
  latMax: number;
  lonMin: number;
  lonMax: number;
} {
  const lat1 = grid.lat0 + grid.dlat * (grid.nlat - 1);
  const lon1 = grid.lon0 + grid.dlon * (grid.nlon - 1);
  return {
    latMin: Math.min(grid.lat0, lat1),
    latMax: Math.max(grid.lat0, lat1),
    lonMin: Math.min(grid.lon0, lon1),
    lonMax: Math.max(grid.lon0, lon1),
  };
}

export function inGrid(grid: MeteoGrid, lat: number, lon: number): boolean {
  const r = gridRect(grid);
  return lat >= r.latMin && lat <= r.latMax && lon >= r.lonMin && lon <= r.lonMax;
}

/** Advance (or reverse) along the field by `dtSec` seconds of real wind. */
export function stepFlow(
  grid: MeteoGrid,
  lat: number,
  lon: number,
  dtSec: number,
): FlowStep | null {
  if (!inGrid(grid, lat, lon)) return null;
  const { u, v, speed } = sampleGrid(grid, lat, lon);
  if (speed < MIN_SPEED_KN) return null;
  const latRad = (lat * Math.PI) / 180;
  const cosLat = Math.max(0.15, Math.cos(latRad));
  const nextLat = lat + (v * dtSec) / METERS_PER_DEG_LAT;
  const nextLon = lon + (u * dtSec) / (METERS_PER_DEG_LAT * cosLat);
  if (!inGrid(grid, nextLat, nextLon)) return null;
  return { lat: nextLat, lon: nextLon, u, v, speed };
}

/**
 * Euler step sized so typical wind travels ~`stepPx` on screen.
 * `sign` -1 traces upstream.
 */
export function stepFlowPx(
  grid: MeteoGrid,
  lat: number,
  lon: number,
  stepPx: number,
  zoom: number,
  sign = 1,
): FlowStep | null {
  if (!inGrid(grid, lat, lon)) return null;
  const { u, v, speed } = sampleGrid(grid, lat, lon);
  if (speed < MIN_SPEED_KN) return null;
  const speedMs = Math.hypot(u, v);
  if (speedMs < 0.05) return null;
  const dt = ((stepPx * metersPerPixel(lat, zoom)) / speedMs) * sign;
  return stepFlow(grid, lat, lon, dt);
}

/** Opaque head of a comet (px). Independent of zoom. */
export const COMET_HEAD_PX = 5;
/** Tail length in px at 0 kn → 60 kn (plus head). */
export const COMET_TAIL_MIN_PX = 10;
export const COMET_TAIL_MAX_PX = 28;

/** Screen-space comet size from TWS (same 0–60 kn domain as the colour bar). */
export function cometLengthPx(speedKn: number): {
  headPx: number;
  tailPx: number;
  totalPx: number;
} {
  const t = Math.max(0, Math.min(60, speedKn)) / 60;
  const tailPx = COMET_TAIL_MIN_PX + t * (COMET_TAIL_MAX_PX - COMET_TAIL_MIN_PX);
  return { headPx: COMET_HEAD_PX, tailPx, totalPx: COMET_HEAD_PX + tailPx };
}

export function destPoint(
  lat: number,
  lon: number,
  bearingDeg: number,
  distM: number,
): { lat: number; lon: number } {
  const rad = (bearingDeg * Math.PI) / 180;
  const cosLat = Math.max(0.15, Math.cos((lat * Math.PI) / 180));
  return {
    lat: lat + (Math.cos(rad) * distM) / METERS_PER_DEG_LAT,
    lon: lon + (Math.sin(rad) * distM) / (METERS_PER_DEG_LAT * cosLat),
  };
}

export function hash01(a: number, b: number): number {
  const x = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return x - Math.floor(x);
}
