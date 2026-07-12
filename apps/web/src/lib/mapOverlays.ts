/** Shared map overlay helpers. */

import type { Course, LatLon, RouteLeg } from "./types";

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

export function courseIsReady(course: Course | null | undefined): boolean {
  if (!course) return false;
  return (
    Array.isArray(course.start) &&
    course.start.length === 2 &&
    Array.isArray(course.finish) &&
    course.finish.length === 2 &&
    !(course.start[0] === 0 && course.start[1] === 0 && course.finish[0] === 0 && course.finish[1] === 0)
  );
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

export interface LegAtTime {
  position: LatLon;
  heading: number | null;
  leg: RouteLeg;
  index: number;
}

/** Interpolate boat state between route legs at an absolute UTC time. */
export function legAtTime(legs: RouteLeg[], tUtc: string): LegAtTime | null {
  if (!legs.length) return null;
  const tMs = new Date(tUtc).getTime();
  if (Number.isNaN(tMs)) {
    const leg = legs[0];
    return {
      position: leg.position,
      heading: leg.cog ?? leg.heading ?? null,
      leg,
      index: 0,
    };
  }

  if (tMs <= new Date(legs[0].t_utc).getTime()) {
    const leg = legs[0];
    return {
      position: leg.position,
      heading: leg.cog ?? leg.heading ?? null,
      leg,
      index: 0,
    };
  }
  const last = legs.length - 1;
  if (tMs >= new Date(legs[last].t_utc).getTime()) {
    const leg = legs[last];
    return {
      position: leg.position,
      heading: leg.cog ?? leg.heading ?? null,
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
  const heading = lerpAngle(a.cog ?? a.heading ?? 0, b.cog ?? b.heading ?? 0);
  const leg: RouteLeg = {
    ...a,
    t_utc: tUtc,
    position,
    tws: lerp(a.tws, b.tws),
    twd: lerpAngle(a.twd, b.twd),
    twa: lerp(a.twa, b.twa),
    heading,
    bsp: lerp(a.bsp, b.bsp),
    sog: lerp(a.sog, b.sog),
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
