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

export function inGrid(grid: MeteoGrid, lat: number, lon: number): boolean {
  const lat1 = grid.lat0 + grid.dlat * (grid.nlat - 1);
  const lon1 = grid.lon0 + grid.dlon * (grid.nlon - 1);
  const latMin = Math.min(grid.lat0, lat1);
  const latMax = Math.max(grid.lat0, lat1);
  const lonMin = Math.min(grid.lon0, lon1);
  const lonMax = Math.max(grid.lon0, lon1);
  return lat >= latMin && lat <= latMax && lon >= lonMin && lon <= lonMax;
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
