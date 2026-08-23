/** Preset Isle of Wight / Solent courses for the routing lab. */

import type { Course, LatLon, Mark } from "./types";

export interface LabCourse {
  id: string;
  title: string;
  blurb: string;
  course: Course;
}

// Mid-channel, not the towns — Yarmouth village and Cowes harbour are dry/too shallow.
const YARMOUTH: LatLon = [50.73, -1.505];
const COWES: LatLon = [50.778, -1.3];
const SOLENT_TWD_FALLBACK = 270; // westerly — beat is east → west

function mark(position: LatLon, radiusNm: number, name: string): Mark {
  return { position, radius_nm: radiusNm, rounding: "either", name };
}

function course(
  start: LatLon,
  finish: LatLon,
  departUtc: string,
  extra?: Partial<Course>,
): Course {
  return {
    start,
    finish,
    marks: [],
    exclusions: [],
    depart_utc: departUtc,
    min_depth_m: 3,
    ...extra,
  };
}

function absCircDiff(a: number, b: number): number {
  const d = Math.abs(a - b) % 360;
  return d > 180 ? 360 - d : d;
}

function bearingDeg(a: LatLon, b: LatLon): number {
  const lat1 = (a[0] * Math.PI) / 180;
  const lat2 = (b[0] * Math.PI) / 180;
  const dLon = ((b[1] - a[1]) * Math.PI) / 180;
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

/** Pick start/finish so the leg is upwind (toward TWD) or downwind. */
function orientPair(
  west: LatLon,
  east: LatLon,
  twd: number,
  wantUpwind: boolean,
): { start: LatLon; finish: LatLon } {
  const eastbound = bearingDeg(west, east);
  const westbound = bearingDeg(east, west);
  const eastIsUpwind = absCircDiff(eastbound, twd) <= absCircDiff(westbound, twd);
  const upwind = eastIsUpwind
    ? { start: west, finish: east }
    : { start: east, finish: west };
  if (wantUpwind) return upwind;
  return { start: upwind.finish, finish: upwind.start };
}

export function buildLabCourses(twd: number | null, departUtc: string): LabCourse[] {
  const wind = twd == null || !Number.isFinite(twd) ? SOLENT_TWD_FALLBACK : twd;
  const beat = orientPair(YARMOUTH, COWES, wind, true);
  const run = orientPair(YARMOUTH, COWES, wind, false);

  return [
    {
      id: "solent-upwind",
      title: "Western Solent beat",
      blurb: "Yarmouth Roads ↔ Cowes, oriented upwind for the current GFS TWD.",
      course: course(beat.start, beat.finish, departUtc),
    },
    {
      id: "solent-downwind",
      title: "Western Solent run",
      blurb: "Same axis reversed — a downwind / reaching return.",
      course: course(run.start, run.finish, departUtc),
    },
    {
      id: "cowes-short",
      title: "Cowes short-leg",
      blurb: "Tight angles and close marks just north of Cowes.",
      course: course([50.778, -1.31], [50.775, -1.29], departUtc, {
        marks: [
          mark([50.785, -1.295], 0.05, "M1"),
          mark([50.78, -1.28], 0.05, "M2"),
        ],
      }),
    },
    {
      id: "south-reach",
      title: "South-side reach",
      blurb: "Long open reaching legs south of the island, well offshore.",
      course: course([50.545, -1.20], [50.55, -1.53], departUtc, {
        marks: [mark([50.535, -1.35], 0.12, "M1")],
      }),
    },
    {
      id: "hurst-shallow",
      title: "Hurst Narrows",
      blurb: "Narrow, shallow passage from the Needles Channel toward Newtown.",
      course: course([50.705, -1.575], [50.745, -1.43], departUtc, {
        marks: [mark([50.712, -1.54], 0.08, "M1")],
        min_depth_m: 1.5,
      }),
    },
    {
      id: "needles-headland",
      title: "Needles rounding",
      blurb: "Headland rounding of the Needles, then east along the south shore.",
      course: course([50.705, -1.575], [50.64, -1.48], departUtc, {
        marks: [mark([50.662, -1.62], 0.2, "Needles")],
      }),
    },
  ];
}

export function coursePoints(c: Course): LatLon[] {
  return [c.start, ...c.marks.map((m) => m.position), c.finish];
}
