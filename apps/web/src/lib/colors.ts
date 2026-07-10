// Distinct per-model colors for the PredictWind-style overlay. Mirrors the
// `color` field of the backend ModelPreset list (services/api/app/config.py).

export const MODEL_COLORS: Record<string, string> = {
  ecmwf: "#2dd4bf", // teal
  icon: "#f59e0b", // amber
  icon_eu: "#a78bfa", // violet
  arpege: "#f472b6", // pink
  arome: "#4ade80", // green
  ukmo: "#60a5fa", // blue
};

export const FALLBACK_COLORS = [
  "#2dd4bf",
  "#f59e0b",
  "#a78bfa",
  "#f472b6",
  "#4ade80",
  "#60a5fa",
  "#facc15",
  "#fb7185",
];

export function colorForModel(id: string, index = 0): string {
  return MODEL_COLORS[id] ?? FALLBACK_COLORS[index % FALLBACK_COLORS.length];
}
