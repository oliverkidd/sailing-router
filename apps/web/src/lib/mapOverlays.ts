/** Shared map overlay helpers. */

import type { Course } from "./types";

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

export function emptyCourse(departUtc: string): Course {
  return {
    start: [0, 0],
    finish: [0, 0],
    marks: [],
    exclusions: [],
    depart_utc: departUtc,
    min_depth_m: 3,
  };
}
