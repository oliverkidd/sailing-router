/** Throwaway chart boxes for the /coverage sign-off page. */

export interface CoverageBox {
  id: string;
  name: string;
  blurb: string;
  color: string;
  lonMin: number;
  latMin: number;
  lonMax: number;
  latMax: number;
}

/** Gabes, Tunisia. Both Med boxes stop on this parallel. */
const GABES_LAT = 33.9;

export const COVERAGE_BOXES: CoverageBox[] = [
  {
    id: "channel",
    name: "Channel & Fastnet",
    blurb: "Fastnet, Cork, Scillies, Solent, Cherbourg, Dover",
    color: "#0f766e",
    lonMin: -10.8,
    latMin: 48.2,
    lonMax: 1.6,
    latMax: 52.2,
  },
  {
    id: "france-iberia",
    name: "France, Spain & Portugal",
    blurb: "All of France, Spain, and Portugal, including Biscay and the Atlantic coast",
    color: "#6d28d9",
    lonMin: -9.8,
    latMin: 35.9,
    lonMax: 8.4,
    latMax: 51.2,
  },
  {
    id: "med-west",
    name: "Med west",
    blurb: "Gibraltar, Giraglia, Adriatic, Sicily, Malta, Tunisia down to Gabes",
    color: "#1d4ed8",
    lonMin: -5.8,
    latMin: GABES_LAT,
    lonMax: 20.0,
    latMax: 45.9,
  },
  {
    id: "med-east",
    name: "Med east",
    blurb: "Aegean, Crete, Cyprus, south coast of Turkey, down to the Gabes parallel",
    color: "#c2410c",
    lonMin: 19.5,
    latMin: GABES_LAT,
    lonMax: 36.4,
    latMax: 41.2,
  },
];

export function boxRing(box: Pick<CoverageBox, "lonMin" | "latMin" | "lonMax" | "latMax">): [number, number][] {
  const { lonMin, latMin, lonMax, latMax } = box;
  return [
    [lonMin, latMin],
    [lonMax, latMin],
    [lonMax, latMax],
    [lonMin, latMax],
    [lonMin, latMin],
  ];
}

export function formatBoxEdges(box: CoverageBox): string {
  const west = box.lonMin < 0 ? `${Math.abs(box.lonMin).toFixed(1)}°W` : `${box.lonMin.toFixed(1)}°E`;
  const east = box.lonMax < 0 ? `${Math.abs(box.lonMax).toFixed(1)}°W` : `${box.lonMax.toFixed(1)}°E`;
  return `${box.latMin.toFixed(1)}–${box.latMax.toFixed(1)}°N, ${west}–${east}`;
}
