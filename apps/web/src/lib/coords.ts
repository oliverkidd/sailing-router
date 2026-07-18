/** Degrees + decimal-minutes helpers for nautical lat/lon entry. */

export type HemNS = "N" | "S";
export type HemEW = "E" | "W";

export interface DmParts {
  deg: number;
  min: number;
  hem: HemNS | HemEW;
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

/** Split a signed decimal degree into integer degrees + decimal minutes + hemisphere. */
export function decimalToDm(value: number, axis: "lat" | "lon"): DmParts {
  const abs = Math.abs(value);
  const deg = Math.floor(abs + 1e-12);
  let min = (abs - deg) * 60;
  // Guard float noise near 60′
  if (min >= 59.9995) {
    return {
      deg: deg + 1,
      min: 0,
      hem: axis === "lat" ? (value >= 0 ? "N" : "S") : value >= 0 ? "E" : "W",
    };
  }
  return {
    deg,
    min,
    hem: axis === "lat" ? (value >= 0 ? "N" : "S") : value >= 0 ? "E" : "W",
  };
}

/** Combine DM parts into signed decimal degrees. */
export function dmToDecimal(parts: DmParts, axis: "lat" | "lon"): number {
  const maxDeg = axis === "lat" ? 90 : 180;
  const deg = clamp(Math.floor(Math.abs(parts.deg)), 0, maxDeg);
  let min = Number.isFinite(parts.min) ? parts.min : 0;
  min = clamp(min, 0, 59.999);
  if (deg === maxDeg) min = 0;
  const abs = deg + min / 60;
  const neg = axis === "lat" ? parts.hem === "S" : parts.hem === "W";
  return neg ? -abs : abs;
}

export function formatMin(min: number): string {
  return min.toFixed(3);
}
