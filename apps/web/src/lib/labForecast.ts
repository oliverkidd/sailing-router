/** Bucket a route by GFS hour so forecast breakpoints are visible per card. */

import { haversineNm } from "./mapOverlays";
import type { RouteLeg } from "./types";

const HOUR_MS = 3_600_000;

export interface ForecastHourBucket {
  /** Floored UTC hour the boat was sailing in. */
  hourUtc: string;
  label: string;
  distanceNm: number;
  twdDeg: number;
  twsKn: number;
}

export interface ForecastShift {
  buckets: ForecastHourBucket[];
  /** Number of GFS hour boundaries the track actually crossed. */
  crossings: number;
  /** Largest |ΔTWD| between consecutive hours the boat sailed in. */
  maxShiftDeg: number;
}

export function floorHourMs(iso: string): number {
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? Number.NaN : Math.floor(ms / HOUR_MS) * HOUR_MS;
}

export function hourTagUtc(ms: number): string {
  return `${String(new Date(ms).getUTCHours()).padStart(2, "0")}Z`;
}

/** Shortest signed turn from a to b, in degrees (−180, 180]. */
export function angleDeltaDeg(a: number, b: number): number {
  return ((b - a + 540) % 360) - 180;
}

export function forecastShift(legs: RouteLeg[]): ForecastShift {
  type Acc = { dist: number; x: number; y: number; tws: number; weight: number };
  const acc = new Map<number, Acc>();

  const add = (hourMs: number, distNm: number, twd: number, tws: number) => {
    if (!Number.isFinite(hourMs)) return;
    const cur = acc.get(hourMs) ?? { dist: 0, x: 0, y: 0, tws: 0, weight: 0 };
    // Distance-weighted so a long tack dominates the hour's reported wind, but
    // a zero-length sample still contributes its direction.
    const weight = Math.max(distNm, 1e-6);
    const rad = (twd * Math.PI) / 180;
    cur.dist += distNm;
    cur.x += Math.cos(rad) * weight;
    cur.y += Math.sin(rad) * weight;
    cur.tws += tws * weight;
    cur.weight += weight;
    acc.set(hourMs, cur);
  };

  if (legs.length === 1) {
    add(floorHourMs(legs[0].t_utc), 0, legs[0].twd, legs[0].tws);
  }
  for (let i = 0; i < legs.length - 1; i++) {
    const a = legs[i];
    const b = legs[i + 1];
    add(floorHourMs(a.t_utc), haversineNm(a.position, b.position), a.twd, a.tws);
  }

  const buckets: ForecastHourBucket[] = Array.from(acc.entries())
    .sort((x, y) => x[0] - y[0])
    .map(([hourMs, a]) => ({
      hourUtc: new Date(hourMs).toISOString(),
      label: hourTagUtc(hourMs),
      distanceNm: a.dist,
      twdDeg: ((Math.atan2(a.y, a.x) * 180) / Math.PI + 360) % 360,
      twsKn: a.weight > 0 ? a.tws / a.weight : 0,
    }));

  let maxShiftDeg = 0;
  for (let i = 1; i < buckets.length; i++) {
    const d = Math.abs(angleDeltaDeg(buckets[i - 1].twdDeg, buckets[i].twdDeg));
    if (d > maxShiftDeg) maxShiftDeg = d;
  }

  return { buckets, crossings: Math.max(0, buckets.length - 1), maxShiftDeg };
}
