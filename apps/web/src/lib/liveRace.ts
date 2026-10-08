import type { LatLon, RouteLeg } from "@/lib/types";
import { haversineNm, legAtTime, type LegAtTime } from "@/lib/mapOverlays";

const NM_PER_DEG_LAT = 60;

export interface GpsFix {
  position: LatLon;
  /** Horizontal accuracy in metres, when the browser provides it. */
  accuracyM: number | null;
  /** Degrees true, clockwise from north. Null when the device has no course. */
  headingDeg: number | null;
  speedKn: number | null;
  atUtc: string;
}

export type RacePhase = "no-route" | "before-start" | "racing" | "finished";
export type CrossTrackSide = "port" | "stbd" | "on";

export interface AlongTrack {
  position: LatLon;
  tUtc: string;
  index: number;
  crossTrackNm: number;
  side: CrossTrackSide;
}

export interface RaceSnapshot {
  phase: RacePhase;
  /** Where the planned boat is at `nowUtc`. */
  plan: LegAtTime | null;
  /** Nearest point on the planned track to the GPS fix. */
  along: AlongTrack | null;
  /**
   * Seconds the boat is ahead of the plan. Positive means the point on the
   * route nearest the boat is later than now: you have reached a place the
   * plan does not reach until then.
   */
  aheadBehindS: number | null;
  departPassed: boolean;
}

function localNm(origin: LatLon, point: LatLon): { e: number; n: number } {
  const cos = Math.max(0.2, Math.cos((origin[0] * Math.PI) / 180));
  return {
    e: (point[1] - origin[1]) * NM_PER_DEG_LAT * cos,
    n: (point[0] - origin[0]) * NM_PER_DEG_LAT,
  };
}

/** Project `gps` onto the route and return the closest point and its planned time. */
export function nearestOnRoute(legs: RouteLeg[], gps: LatLon): AlongTrack | null {
  if (!legs.length) return null;
  if (legs.length === 1) {
    const only = legs[0];
    return {
      position: only.position,
      tUtc: only.t_utc,
      index: 0,
      crossTrackNm: haversineNm(gps, only.position),
      side: "on",
    };
  }

  let bestDist = Infinity;
  let best: AlongTrack | null = null;
  for (let i = 0; i < legs.length - 1; i++) {
    const a = legs[i].position;
    const b = legs[i + 1].position;
    const ab = localNm(a, b);
    const ag = localNm(a, gps);
    const len2 = ab.e * ab.e + ab.n * ab.n;
    let t = len2 < 1e-8 ? 0 : (ag.e * ab.e + ag.n * ab.n) / len2;
    t = Math.max(0, Math.min(1, t));
    const proj: LatLon = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    const dist = haversineNm(gps, proj);
    if (dist >= bestDist) continue;
    bestDist = dist;
    const t0 = new Date(legs[i].t_utc).getTime();
    const t1 = new Date(legs[i + 1].t_utc).getTime();
    const at = t0 + (t1 - t0) * t;
    // Positive cross is left of the track (port). East of a northbound leg is starboard.
    const z = ab.e * ag.n - ab.n * ag.e;
    best = {
      position: proj,
      tUtc: new Date(at).toISOString(),
      index: i,
      crossTrackNm: dist,
      side: dist < 0.02 ? "on" : z > 0 ? "port" : "stbd",
    };
  }
  return best;
}

export function compareToRoute(
  legs: RouteLeg[] | null | undefined,
  gps: LatLon | null,
  nowUtc: string,
  departUtc: string,
): RaceSnapshot {
  const nowMs = new Date(nowUtc).getTime();
  const departMs = new Date(departUtc).getTime();
  const departPassed = !Number.isNaN(departMs) && !Number.isNaN(nowMs) && nowMs >= departMs;
  const plan = legs?.length ? legAtTime(legs, nowUtc) : null;
  const along = legs?.length && gps ? nearestOnRoute(legs, gps) : null;

  let phase: RacePhase = "no-route";
  if (legs?.length) {
    const startMs = new Date(legs[0].t_utc).getTime();
    const endMs = new Date(legs[legs.length - 1].t_utc).getTime();
    if (!Number.isNaN(nowMs) && nowMs < startMs) phase = "before-start";
    else if (!Number.isNaN(nowMs) && nowMs > endMs) phase = "finished";
    else phase = "racing";
  }

  let aheadBehindS: number | null = null;
  if (along && !Number.isNaN(nowMs)) {
    const atMs = new Date(along.tUtc).getTime();
    if (!Number.isNaN(atMs)) aheadBehindS = (atMs - nowMs) / 1000;
  }

  return { phase, plan, along, aheadBehindS, departPassed };
}

export function formatAheadBehind(seconds: number | null): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const ahead = seconds >= 0;
  const abs = Math.abs(seconds);
  const word = ahead ? "ahead" : "behind";
  if (abs < 45) return `on time`;
  if (abs < 3600) return `${Math.round(abs / 60)} min ${word}`;
  const h = Math.floor(abs / 3600);
  const m = Math.round((abs % 3600) / 60);
  return m > 0 ? `${h}h ${m}m ${word}` : `${h}h ${word}`;
}

export function formatCrossTrack(along: AlongTrack | null): string {
  if (!along) return "—";
  if (along.side === "on" || along.crossTrackNm < 0.02) return "on the route";
  const nm = along.crossTrackNm;
  const dist = nm < 0.2 ? `${Math.round(nm * 1852)} m` : `${nm.toFixed(2)} nm`;
  return `${dist} to ${along.side}`;
}

export function formatStartsIn(departUtc: string, nowUtc: string): string {
  const delta = new Date(departUtc).getTime() - new Date(nowUtc).getTime();
  if (Number.isNaN(delta)) return "";
  if (delta >= 0) {
    const min = Math.round(delta / 60000);
    if (min < 1) return "starts now";
    if (min < 60) return `starts in ${min} min`;
    const h = Math.floor(min / 60);
    return `starts in ${h}h ${min % 60}m`;
  }
  const ago = Math.round(-delta / 60000);
  if (ago < 60) return `started ${ago} min ago`;
  const h = Math.floor(ago / 60);
  return `started ${h}h ${ago % 60}m ago`;
}
