/**
 * Viewport helpers for browse wind.
 *
 * Browse density is locked to a Terrain toggle, not the camera zoom: standard
 * uses the fine tier, Terrain uses ultra + WindNinja. The server still pads
 * the request once; cache bookkeeping uses the returned series.bbox.
 */

import type { MapBounds } from "./mapOverlays";

export const FORECAST_MAX_SPAN_MI = 300;

/** Mirrors services/api/app/wind/zoom_tiers.py tier_for_zoom. */
export type ZoomTierName = "coarse" | "mid" | "fine" | "ultra";

const TIER_RANK: Record<ZoomTierName, number> = {
  coarse: 0,
  mid: 1,
  fine: 2,
  ultra: 3,
};

/** Map MapLibre zoom → display tier name (must match server). */
export function tierForZoom(zoom: number | null | undefined): ZoomTierName {
  const z = zoom == null || !Number.isFinite(zoom) ? 5 : zoom;
  if (z <= 6) return "coarse";
  if (z <= 9) return "mid";
  if (z <= 12) return "fine";
  return "ultra";
}

/** Synthetic /wind/area zoom: camera zoom must not change display density. */
export const BROWSE_ZOOM_STANDARD = 11; // fine (~500 m)
export const BROWSE_ZOOM_HIGH_DEF = 13; // ultra (~200 m)

export function browseFetchZoom(highDef: boolean): number {
  return highDef ? BROWSE_ZOOM_HIGH_DEF : BROWSE_ZOOM_STANDARD;
}

export function tierRank(tier: string | null | undefined): number {
  if (tier && tier in TIER_RANK) return TIER_RANK[tier as ZoomTierName];
  return -1;
}

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
 * True when the current zoom needs a finer display tier than the cached series.
 * Browse no longer upgrades density on zoom; kept for debug / older callers.
 */
export function regionNeedsDenserReplace(
  cachedTier: string | null | undefined,
  zoom: number | null | undefined,
): boolean {
  const need = tierRank(tierForZoom(zoom));
  const have = tierRank(cachedTier);
  if (have < 0) return true;
  return need > have;
}

/** @deprecated Server pads once; prefer sending the raw viewport. */
export const REGION_PAD_DEG = 2.0;
export const REGION_SNAP_DEG = 2.0;

/** Expand + snap viewport (legacy helper; browse no longer pre-pads). */
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
