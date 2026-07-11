/**
 * Viewport span helpers (LOD tile pyramid removed — wind comes from /wind/area).
 */

import type { MapBounds } from "./mapOverlays";

export const FORECAST_MAX_SPAN_MI = 300;

/** Haversine distance in miles between two lat/lon points. */
export function haversineMiles(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 3958.8;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Max of N–S and E–W viewport spans in miles. */
export function viewportMaxSpanMiles(b: MapBounds): number {
  const midLat = (b.lat_min + b.lat_max) / 2;
  const midLon = (b.lon_min + b.lon_max) / 2;
  const ns = haversineMiles(b.lat_min, midLon, b.lat_max, midLon);
  const ew = haversineMiles(midLat, b.lon_min, midLat, b.lon_max);
  return Math.max(ns, ew);
}

/** Round time to the hour for stable cache keys. */
export function roundHourUtc(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  d.setUTCMinutes(0, 0, 0);
  return d.toISOString();
}

/** Region cell key for models/available caching (0.5°). */
export function regionCellKey(b: MapBounds): string {
  const q = (v: number) => Math.floor(v * 2) / 2;
  return `${q(b.lat_min)}_${q(b.lon_min)}_${q(b.lat_max)}_${q(b.lon_max)}`;
}

/** True if `inner` is fully contained in `outer`. */
export function bboxContains(outer: MapBounds, inner: MapBounds, margin = 0): boolean {
  return (
    outer.lat_min - margin <= inner.lat_min &&
    outer.lon_min - margin <= inner.lon_min &&
    outer.lat_max + margin >= inner.lat_max &&
    outer.lon_max + margin >= inner.lon_max
  );
}
