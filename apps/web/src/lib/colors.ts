/** Windy-style speed color palette (0–40 kn). */

export function windyColor(twsKn: number): string {
  const t = Math.max(0, Math.min(40, twsKn)) / 40;
  let r: number, g: number, b: number;
  if (t < 0.25) {
    r = 0;
    g = Math.floor(100 + t * 4 * 155);
    b = 255;
  } else if (t < 0.5) {
    r = 0;
    g = 255;
    b = Math.floor(255 - (t - 0.25) * 4 * 255);
  } else if (t < 0.75) {
    r = Math.floor((t - 0.5) * 4 * 255);
    g = 255;
    b = 0;
  } else {
    r = 255;
    g = Math.floor(255 - (t - 0.75) * 4 * 255);
    b = 0;
  }
  return `rgb(${r},${g},${b})`;
}

export const MODEL_COLORS: Record<string, string> = {
  gfs: "#ef4444",
  ecmwf: "#2dd4bf",
  icon: "#f59e0b",
  ukmo: "#60a5fa",
  gem: "#a78bfa",
  icon_eu: "#fbbf24",
  icon_d2: "#fcd34d",
  arome: "#f472b6",
  arpege: "#fb7185",
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
