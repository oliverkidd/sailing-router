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

/** Lat×lon span product (degrees²) for region size comparisons. */
export function bboxAreaDeg2(b: MapBounds): number {
  return Math.max(0, b.lat_max - b.lat_min) * Math.max(0, b.lon_max - b.lon_min);
}

/**
 * True when `next` is meaningfully smaller than `cached` (zoom-in).
 * Browse should force-replace rather than reuse the coarse large-region series.
 */
export function regionNeedsDenserReplace(
  cached: MapBounds,
  next: MapBounds,
  areaRatio = 0.5,
): boolean {
  const a = bboxAreaDeg2(cached);
  if (a <= 0) return false;
  return bboxAreaDeg2(next) / a < areaRatio;
}

/** Matches server REGION_PAD / REGION_SNAP in services/api/app/util/bbox.py. */
export const REGION_PAD_DEG = 2.0;
export const REGION_SNAP_DEG = 2.0;

/** Expand + snap viewport to the server GRIB cache cell (pad 2°, snap 2°). */
export function regionBounds(
  bbox: MapBounds,
  padDeg = REGION_PAD_DEG,
  snapDeg = REGION_SNAP_DEG,
): MapBounds {
  let lat_min = Math.floor((bbox.lat_min - padDeg) / snapDeg) * snapDeg;
  let lon_min = Math.floor((bbox.lon_min - padDeg) / snapDeg) * snapDeg;
  let lat_max = Math.ceil((bbox.lat_max + padDeg) / snapDeg) * snapDeg;
  let lon_max = Math.ceil((bbox.lon_max + padDeg) / snapDeg) * snapDeg;
  if (lat_max <= lat_min) lat_max = lat_min + snapDeg;
  if (lon_max <= lon_min) lon_max = lon_min + snapDeg;
  return {
    lat_min: Math.round(lat_min * 10000) / 10000,
    lon_min: Math.round(lon_min * 10000) / 10000,
    lat_max: Math.round(lat_max * 10000) / 10000,
    lon_max: Math.round(lon_max * 10000) / 10000,
  };
}
