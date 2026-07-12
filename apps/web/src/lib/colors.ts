/** Shared wind-speed domain for heatmap colours + arrow motion (knots). */
export const WIND_SPEED_MAX_KN = 60;

/** Mid-scale cycle duration — rateMul=1 at t=0.5 (30 kn). */
export const WIND_ARROW_CYCLE_MS_MID = 1900;

/** Normalize knots onto the legend / heatmap domain → 0..1. */
export function windSpeedNorm(twsKn: number): number {
  return Math.max(0, Math.min(WIND_SPEED_MAX_KN, twsKn)) / WIND_SPEED_MAX_KN;
}

/**
 * Wind-speed palette (0–60 kn).
 * 0 grey → 5 dark blue → 10 light blue → 15 green → 20 yellow → 30 orange → 40 red → 50 purple → 60 dark purple
 */
const WIND_STOPS: { kn: number; rgb: [number, number, number] }[] = [
  { kn: 0, rgb: [110, 118, 130] }, // grey — lighter so calm still reads
  { kn: 5, rgb: [40, 90, 220] }, // dark blue — richer/brighter
  { kn: 10, rgb: [90, 200, 255] }, // light blue
  { kn: 15, rgb: [60, 245, 90] }, // green
  { kn: 20, rgb: [255, 240, 50] }, // yellow
  { kn: 30, rgb: [255, 160, 35] }, // orange
  { kn: 40, rgb: [255, 55, 55] }, // red
  { kn: 50, rgb: [210, 55, 230] }, // purple
  { kn: 60, rgb: [100, 35, 160] }, // dark purple — still deep but less muddy
];

function lerpByte(a: number, b: number, t: number): number {
  return Math.round(a + (b - a) * t);
}

function windRgb(twsKn: number): [number, number, number] {
  const kn = Math.max(0, twsKn);
  if (kn <= WIND_STOPS[0].kn) return WIND_STOPS[0].rgb;
  for (let i = 1; i < WIND_STOPS.length; i++) {
    const lo = WIND_STOPS[i - 1];
    const hi = WIND_STOPS[i];
    if (kn <= hi.kn) {
      const u = (kn - lo.kn) / (hi.kn - lo.kn);
      return [
        lerpByte(lo.rgb[0], hi.rgb[0], u),
        lerpByte(lo.rgb[1], hi.rgb[1], u),
        lerpByte(lo.rgb[2], hi.rgb[2], u),
      ];
    }
  }
  return WIND_STOPS[WIND_STOPS.length - 1].rgb;
}

export function windyColor(twsKn: number): string {
  const [r, g, b] = windRgb(twsKn);
  return `rgb(${r},${g},${b})`;
}

export type WindScale = {
  /** 0..1 on the legend domain (0–60 kn). */
  t: number;
  /** Heatmap / legend colour. */
  color: string;
  /** RGB channels matching `color` (avoids re-parse in canvas heatmap). */
  rgb: [number, number, number];
  /**
   * Arrow motion — all linear in `t` on the same domain as the colour bar.
   * - rateMul: cycle speed vs mid (1 at 30 kn); high end ~3× calm
   * - travelPx: drift distance this cycle (~1×→~3× arrow length)
   * - keepProb: sample keep-rate (~⅓→1) for local density
   * - cycleMs: absolute cycle duration (= mid / rateMul)
   */
  rateMul: number;
  travelPx: number;
  keepProb: number;
  cycleMs: number;
};

/**
 * Single source of truth for wind visuals: colour + arrow rate/travel/density.
 * Heatmap, legend, and wind arrows all derive from this.
 */
export function windScale(twsKn: number): WindScale {
  const t = windSpeedNorm(twsKn);
  const rgb = windRgb(twsKn);
  // Rate: 0.55× at 0 kn → 1× at mid → 1.75× at 60 kn
  const rateMul = 0.55 + t * 1.2;
  // Travel: 20px (~1×) → 64px (~3.2× arrow length)
  const travelPx = 20 + t * 44;
  // Density: ⅓ → full
  const keepProb = 1 / 3 + t * (2 / 3);
  return {
    t,
    color: `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`,
    rgb,
    rateMul,
    travelPx,
    keepProb,
    cycleMs: WIND_ARROW_CYCLE_MS_MID / rateMul,
  };
}

export const MODEL_COLORS: Record<string, string> = {
  gfs: "#ef4444",
  ecmwf: "#2dd4bf",
  aifs: "#14b8a6",
  gem: "#a78bfa",
  icon_eu: "#fbbf24",
  icon_d2: "#fcd34d",
  arome: "#f472b6",
  arpege_eu: "#fda4af",
  harmonie: "#38bdf8",
};

export const FALLBACK_COLORS = [
  "#2dd4bf",
  "#f59e0b",
  "#ef4444",
  "#f472b6",
  "#60a5fa",
  "#facc15",
  "#fb7185",
  "#a78bfa",
];

export function colorForModel(id: string, index = 0): string {
  return MODEL_COLORS[id] ?? FALLBACK_COLORS[index % FALLBACK_COLORS.length];
}
