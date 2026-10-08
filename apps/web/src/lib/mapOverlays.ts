/** Shared map overlay helpers. */

import type { Course, LatLon, ModelRoute, RouteLeg } from "./types";

export interface MapBounds {
  lat_min: number;
  lon_min: number;
  lat_max: number;
  lon_max: number;
}

export function boundsFromMap(
  getBounds: () => { getSouth: () => number; getWest: () => number; getNorth: () => number; getEast: () => number },
): MapBounds {
  const b = getBounds();
  return {
    lat_min: b.getSouth(),
    lon_min: b.getWest(),
    lat_max: b.getNorth(),
    lon_max: b.getEast(),
  };
}

export const COURSE_STORAGE_KEY = "sailing-router-course-v1";
export const ROUTE_SESSION_KEY = "sailing-router-route-session-v1";

export function loadStoredCourse(): Course | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(COURSE_STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as Course;
  } catch {
    return null;
  }
}

export function saveStoredCourse(course: Course) {
  if (typeof window === "undefined") return;
  localStorage.setItem(COURSE_STORAGE_KEY, JSON.stringify(course));
}

export interface RouteSessionV1 {
  v: 1;
  courseKey: string;
  savedAt: string;
  routes: import("./types").ModelRoute[];
  warnings: string[];
  ui: {
    activeModelId: string;
    routeIndex: number;
    overlayTimeUtc: string;
    horizonStartUtc: string;
    /** @deprecated Right detail tray retired — ignored when present. */
    detailOpen?: boolean;
  };
}

export function loadStoredRouteSession(): RouteSessionV1 | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(ROUTE_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as RouteSessionV1;
    if (parsed?.v !== 1 || !Array.isArray(parsed.routes)) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveStoredRouteSession(session: RouteSessionV1) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(ROUTE_SESSION_KEY, JSON.stringify(session));
  } catch {
    // Quota exceeded — drop session rather than breaking the app.
    try {
      localStorage.removeItem(ROUTE_SESSION_KEY);
    } catch {
      /* ignore */
    }
  }
}

export function clearStoredRouteSession() {
  if (typeof window === "undefined") return;
  localStorage.removeItem(ROUTE_SESSION_KEY);
}

export function isPointSet(p: LatLon | null | undefined): boolean {
  if (!p || p.length !== 2) return false;
  return !(p[0] === 0 && p[1] === 0);
}

/** Lon/lat box covering set course points and/or route legs (null if empty). */
export function boundsFromCourseAndRoutes(
  course?: Course | null,
  routes?: ModelRoute[] | null,
): MapBounds | null {
  let minLat = Infinity;
  let maxLat = -Infinity;
  let minLon = Infinity;
  let maxLon = -Infinity;
  const add = (lat: number, lon: number) => {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    if (lat === 0 && lon === 0) return;
    minLat = Math.min(minLat, lat);
    maxLat = Math.max(maxLat, lat);
    minLon = Math.min(minLon, lon);
    maxLon = Math.max(maxLon, lon);
  };
  if (course) {
    if (isPointSet(course.start)) add(course.start[0], course.start[1]);
    if (isPointSet(course.finish)) add(course.finish[0], course.finish[1]);
    for (const mk of course.marks) {
      if (isPointSet(mk.position)) add(mk.position[0], mk.position[1]);
    }
  }
  if (routes) {
    for (const r of routes) {
      for (const leg of r.legs) add(leg.position[0], leg.position[1]);
    }
  }
  if (!Number.isFinite(minLat)) return null;
  if (minLat === maxLat) {
    minLat -= 0.02;
    maxLat += 0.02;
  }
  if (minLon === maxLon) {
    minLon -= 0.02;
    maxLon += 0.02;
  }
  return {
    lat_min: minLat,
    lat_max: maxLat,
    lon_min: minLon,
    lon_max: maxLon,
  };
}

/** Finish position: explicit finish, else last placed mark (skip pending [0,0]). */
export function effectiveFinish(course: Course | null | undefined): LatLon | null {
  if (!course) return null;
  if (isPointSet(course.finish)) return course.finish;
  for (let i = course.marks.length - 1; i >= 0; i--) {
    const pos = course.marks[i]?.position;
    if (pos && isPointSet(pos)) return pos;
  }
  return null;
}

/** Course ready when start is set and there is a finish or at least one mark. */
export function courseIsReady(course: Course | null | undefined): boolean {
  if (!course) return false;
  return isPointSet(course.start) && effectiveFinish(course) != null;
}

/** Course with finish filled from last placed mark when finish was left unset. */
export function courseWithEffectiveFinish(course: Course): Course {
  if (isPointSet(course.finish)) return course;
  const fin = effectiveFinish(course);
  if (!fin) return course;
  let lastPlaced = -1;
  for (let i = course.marks.length - 1; i >= 0; i--) {
    if (isPointSet(course.marks[i].position)) {
      lastPlaced = i;
      break;
    }
  }
  if (lastPlaced < 0) return course;
  return {
    ...course,
    finish: fin,
    marks: course.marks
      .filter((_, i) => i !== lastPlaced)
      .filter((m) => isPointSet(m.position)),
  };
}

/** Haversine distance in nautical miles. */
export function haversineNm(a: LatLon, b: LatLon): number {
  const R = 3440.065; // Earth radius in nm
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b[0] - a[0]);
  const dLon = toRad(b[1] - a[1]);
  const x =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(x)));
}

/** Crow-flies course distance through marks: start -> marks -> finish. */
export function courseDistanceNm(course: Course): number {
  if (!isPointSet(course.start) || !isPointSet(course.finish)) return 0;
  const pts: LatLon[] = [
    course.start,
    ...course.marks.map((m) => m.position),
    course.finish,
  ];
  let total = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    total += haversineNm(pts[i], pts[i + 1]);
  }
  return total;
}

/**
 * Cumulative sailed distance along route legs up to `index`, plus the
 * remaining segment from legs[index] to `position` (for scrubber interpolation).
 */
export function cumulativeDistanceNm(
  legs: RouteLeg[],
  index: number,
  position: LatLon,
): number {
  if (!legs.length) return 0;
  let total = 0;
  const end = Math.min(index, legs.length - 1);
  for (let i = 0; i < end; i++) {
    total += haversineNm(legs[i].position, legs[i + 1].position);
  }
  if (end >= 0 && end < legs.length) {
    total += haversineNm(legs[end].position, position);
  }
  return total;
}

export function nextForecastHourUtc(): string {
  const d = new Date();
  d.setUTCMinutes(0, 0, 0);
  d.setUTCHours(d.getUTCHours() + 1);
  return d.toISOString();
}

/** Current UTC hour (floored) — default overlay time. */
export function currentForecastHourUtc(): string {
  const d = new Date();
  d.setUTCMinutes(0, 0, 0);
  return d.toISOString();
}

/** If depart is missing or in the past, bump to the current UTC hour. */
export function ensureFutureDepart(departUtc: string, now = new Date()): string {
  const t = new Date(departUtc).getTime();
  if (Number.isNaN(t) || t < now.getTime()) {
    return currentForecastHourUtc();
  }
  return departUtc;
}

export function emptyCourse(departUtc: string): Course {
  return {
    start: [0, 0],
    finish: [0, 0],
    marks: [],
    exclusions: [],
    depart_utc: ensureFutureDepart(departUtc),
    min_depth_m: 3,
  };
}

/** Keep mark.name in lockstep with panel/map order (M1…Mn). */
export function withSequentialMarkNames<T extends { name?: string | null }>(
  marks: T[],
): T[] {
  return marks.map((m, i) => ({ ...m, name: `M${i + 1}` }));
}

export interface LegAtTime {
  position: LatLon;
  heading: number | null;
  leg: RouteLeg;
  index: number;
}

/** Great-circle bearing from a → b in degrees [0, 360). */
export function segmentBearingDeg(a: LatLon, b: LatLon): number {
  const lat1 = (a[0] * Math.PI) / 180;
  const lat2 = (b[0] * Math.PI) / 180;
  const dLon = ((b[1] - a[1]) * Math.PI) / 180;
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

/**
 * Last leg at or before `tUtc`. Prefer this over nearest-time when marks share
 * a timestamp with their arrival node (router sets mark.t = arrival.t).
 */
export function floorLegIndex(legs: RouteLeg[], tUtc: string): number {
  if (!legs.length) return -1;
  const tMs = new Date(tUtc).getTime();
  if (Number.isNaN(tMs)) return 0;
  if (tMs < new Date(legs[0].t_utc).getTime()) return 0;
  let idx = 0;
  for (let i = 1; i < legs.length; i++) {
    if (new Date(legs[i].t_utc).getTime() <= tMs) idx = i;
    else break;
  }
  return idx;
}

/** Bearing of the route segment the boat is on (matches the red line). */
function routeSegmentHeading(legs: RouteLeg[], index: number): number | null {
  if (legs.length < 2) {
    const leg = legs[0];
    return leg?.cog ?? leg?.heading ?? null;
  }
  const i0 = Math.max(0, Math.min(index, legs.length - 2));
  const a = legs[i0].position;
  const b = legs[i0 + 1].position;
  const dlat = b[0] - a[0];
  const dlon = b[1] - a[1];
  if (Math.abs(dlat) < 1e-12 && Math.abs(dlon) < 1e-12) {
    return legs[i0].cog ?? legs[i0].heading ?? null;
  }
  return segmentBearingDeg(a, b);
}

/** Interpolate boat state between route legs at an absolute UTC time. */
export function legAtTime(legs: RouteLeg[], tUtc: string): LegAtTime | null {
  if (!legs.length) return null;
  const tMs = new Date(tUtc).getTime();
  if (Number.isNaN(tMs)) {
    const leg = legs[0];
    return {
      position: leg.position,
      heading: routeSegmentHeading(legs, 0),
      leg,
      index: 0,
    };
  }

  if (tMs <= new Date(legs[0].t_utc).getTime()) {
    const leg = legs[0];
    return {
      position: leg.position,
      heading: routeSegmentHeading(legs, 0),
      leg,
      index: 0,
    };
  }
  const last = legs.length - 1;
  if (tMs >= new Date(legs[last].t_utc).getTime()) {
    const leg = legs[last];
    return {
      position: leg.position,
      heading: routeSegmentHeading(legs, Math.max(0, last - 1)),
      leg,
      index: last,
    };
  }

  let i0 = 0;
  for (let i = 0; i < last; i++) {
    if (new Date(legs[i + 1].t_utc).getTime() >= tMs) {
      i0 = i;
      break;
    }
  }
  const a = legs[i0];
  const b = legs[i0 + 1];
  const t0 = new Date(a.t_utc).getTime();
  const t1 = new Date(b.t_utc).getTime();
  const ft = t1 === t0 ? 0 : (tMs - t0) / (t1 - t0);
  const position: LatLon = [
    a.position[0] + (b.position[0] - a.position[0]) * ft,
    a.position[1] + (b.position[1] - a.position[1]) * ft,
  ];
  const lerp = (x: number, y: number) => x + (y - x) * ft;
  const lerpAngle = (x: number, y: number) => {
    const d = ((y - x + 540) % 360) - 180;
    return (x + d * ft + 360) % 360;
  };
  // Always point along the red route segment — never lerp heading across a tack.
  const heading = routeSegmentHeading(legs, i0) ?? a.cog ?? a.heading ?? 0;
  // Use the destination leg's speeds so the start node's sog=0 doesn't halve the display.
  const bsp = b.bsp > 0 ? b.bsp : a.bsp;
  const sog = b.sog > 0 ? b.sog : a.sog;
  const leg: RouteLeg = {
    ...a,
    t_utc: tUtc,
    position,
    tws: lerp(a.tws, b.tws),
    twd: lerpAngle(a.twd, b.twd),
    twa: lerp(a.twa, b.twa),
    heading,
    bsp,
    sog,
    cog: heading,
    maneuver: "none",
  };
  return {
    position,
    heading,
    leg,
    index: i0,
  };
}

/**
 * Project a map click onto the nearest route segment and return the
 * interpolated UTC time at that point (or null if the route has no legs).
 */
export function seekTimeAlongRoute(
  legs: RouteLeg[],
  lat: number,
  lon: number,
): string | null {
  if (!legs.length) return null;
  if (legs.length === 1) return legs[0].t_utc;

  let bestDist = Infinity;
  let bestTUtc = legs[0].t_utc;

  for (let i = 0; i < legs.length - 1; i++) {
    const a = legs[i];
    const b = legs[i + 1];
    const ax = a.position[1];
    const ay = a.position[0];
    const bx = b.position[1];
    const by = b.position[0];
    const dx = bx - ax;
    const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    let t = 0;
    if (len2 > 0) {
      t = ((lon - ax) * dx + (lat - ay) * dy) / len2;
      t = Math.max(0, Math.min(1, t));
    }
    const px = ax + dx * t;
    const py = ay + dy * t;
    const dist = (lon - px) * (lon - px) + (lat - py) * (lat - py);
    if (dist < bestDist) {
      bestDist = dist;
      const t0 = new Date(a.t_utc).getTime();
      const t1 = new Date(b.t_utc).getTime();
      bestTUtc = new Date(t0 + (t1 - t0) * t).toISOString();
    }
  }

  return bestTUtc;
}
