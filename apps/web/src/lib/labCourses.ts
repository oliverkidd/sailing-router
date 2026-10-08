/** Preset Isle of Wight / Solent courses for the routing lab. */

import type { Course, LatLon, Mark } from "./types";

export interface LabCourse {
  id: string;
  title: string;
  blurb: string;
  course: Course;
}

/**
 * Mid-channel fixes, not the towns — Yarmouth, Cowes harbour, Newtown creek and
 * Ryde Sands all dry or shoal under the 3 m default clearance, so a start or
 * mark placed on the settlement itself is unroutable.
 */
const NEEDLES_CHANNEL: LatLon = [50.705, -1.575];
const HURST_NARROWS: LatLon = [50.712, -1.54];
const YARMOUTH_ROADS: LatLon = [50.73, -1.505];
const NEWTOWN_APPROACH: LatLon = [50.735, -1.415];
const COWES_ROADS: LatLon = [50.775, -1.31];
const OSBORNE_CHANNEL: LatLon = [50.767, -1.24];
const SPITHEAD: LatLon = [50.762, -1.14];
const NEEDLES: LatLon = [50.655, -1.615];
const ST_CATHERINES: LatLon = [50.555, -1.3];
const DUNNOSE_OFFING: LatLon = [50.585, -1.14];
const SOUTH_SE: LatLon = [50.5, -1.1];
const SOUTH_VENTNOR: LatLon = [50.52, -1.28];
const SOUTH_ATHERFIELD: LatLon = [50.5, -1.45];
const SOUTH_SW: LatLon = [50.53, -1.6];

/** Windward–leeward lap axis in the 16 m mid-Solent channel, clear of Bramble Bank. */
const LAP_LEEWARD: LatLon = [50.75, -1.415];
const LAP_WINDWARD: LatLon = [50.756, -1.372];
const LAP_OFFSET: LatLon = [50.758, -1.374];
const LAP_COUNT = 4;

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

/** Ordered fixes → course; every interior point becomes a loose gate mark. */
function chainCourse(
  chain: LatLon[],
  departUtc: string,
  gateRadiusNm: number,
  extra?: Partial<Course>,
): Course {
  const interior = chain.slice(1, -1);
  return course(chain[0], chain[chain.length - 1], departUtc, {
    marks: interior.map((p, i) => mark(p, gateRadiusNm, `M${i + 1}`)),
    ...extra,
  });
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

/** Order a chain so its overall axis points upwind (toward TWD) or downwind. */
function orientChain(chain: LatLon[], twd: number, wantUpwind: boolean): LatLon[] {
  const last = chain.length - 1;
  const forward = bearingDeg(chain[0], chain[last]);
  const reverse = bearingDeg(chain[last], chain[0]);
  const forwardIsUpwind = absCircDiff(forward, twd) <= absCircDiff(reverse, twd);
  const upwind = forwardIsUpwind ? chain : [...chain].reverse();
  return wantUpwind ? upwind : [...upwind].reverse();
}

/** Repeating windward / offset / leeward roundings, finishing back at leeward. */
function sausageMarks(laps: number): Mark[] {
  const out: Mark[] = [];
  for (let lap = 1; lap <= laps; lap++) {
    out.push(mark(LAP_WINDWARD, 0.05, `W${lap}`));
    out.push(mark(LAP_OFFSET, 0.05, `O${lap}`));
    if (lap < laps) out.push(mark(LAP_LEEWARD, 0.05, `L${lap}`));
  }
  return out;
}

export function buildLabCourses(twd: number | null, departUtc: string): LabCourse[] {
  const wind = twd == null || !Number.isFinite(twd) ? SOLENT_TWD_FALLBACK : twd;
  const axis = [YARMOUTH_ROADS, COWES_ROADS, OSBORNE_CHANNEL, SPITHEAD];
  const beat = orientChain(axis, wind, true);
  const run = orientChain(axis, wind, false);

  return [
    {
      id: "solent-upwind",
      title: "Solent beat (~14 nm)",
      blurb:
        "Yarmouth Roads · Cowes · Osborne · Spithead, ordered upwind for the current GFS TWD. About 3 h of beating, so it crosses several forecast hours.",
      course: chainCourse(beat, departUtc, 0.3),
    },
    {
      id: "solent-downwind",
      title: "Solent run (~14 nm)",
      blurb: "Same axis reversed — a downwind / broad-reaching return over the same forecast window.",
      course: chainCourse(run, departUtc, 0.3),
    },
    {
      id: "cowes-short",
      title: "Sausage laps (~14 nm)",
      blurb: `${LAP_COUNT} windward/leeward laps with an offset mark in the mid-Solent channel — close marks and tight roundings, repeated until it spans two forecast hours.`,
      course: course(LAP_LEEWARD, LAP_LEEWARD, departUtc, {
        marks: sausageMarks(LAP_COUNT),
      }),
    },
    {
      id: "south-reach",
      title: "South-side reach (~20 nm)",
      blurb: "Open reaching legs well offshore of the island's south coast — deep water, no obstructions.",
      course: chainCourse(
        [SOUTH_SE, SOUTH_VENTNOR, SOUTH_ATHERFIELD, SOUTH_SW],
        departUtc,
        0.2,
      ),
    },
    {
      id: "hurst-shallow",
      title: "Hurst Narrows and back (~19 nm)",
      blurb:
        "Round Hurst Spit out of the Needles Channel, up to Newtown and Cowes, then back west. Shallow clearance (1.5 m) both ways through the narrows.",
      course: chainCourse(
        [NEEDLES_CHANNEL, HURST_NARROWS, NEWTOWN_APPROACH, COWES_ROADS, YARMOUTH_ROADS],
        departUtc,
        0.15,
        { min_depth_m: 1.5 },
      ),
    },
    {
      id: "needles-headland",
      title: "Needles to Dunnose (~23 nm)",
      blurb:
        "Round the Needles out of the Needles Channel, then south-east past St Catherine's to Dunnose — a real headland with land across the rhumb line.",
      course: course(NEEDLES_CHANNEL, DUNNOSE_OFFING, departUtc, {
        marks: [mark(NEEDLES, 0.25, "Needles"), mark(ST_CATHERINES, 0.3, "St Catherine's")],
      }),
    },
  ];
}

export function coursePoints(c: Course): LatLon[] {
  return [c.start, ...c.marks.map((m) => m.position), c.finish];
}
