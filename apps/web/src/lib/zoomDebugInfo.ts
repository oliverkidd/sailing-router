/**
 * Temporary zoom debug cheat-sheet — hardcoded mirrors of backend/frontend tiers.
 * Delete with ZoomDebugChip when fact-checking is done.
 */

export type WindTierName = "coarse" | "mid" | "fine" | "ultra";

export interface WindTierInfo {
  name: WindTierName;
  /** Target display grid spacing (degrees). */
  targetResDeg: number;
  /** Human spacing at mid-latitudes. */
  spacingLabel: string;
  windowPadDeg: number;
  maxSide: number;
}

export interface ZoomDebugRow {
  label: string;
  value: string;
}

// source: services/api/app/wind/zoom_tiers.py (tier_for_zoom)
export function tierForZoom(zoom: number): WindTierInfo {
  const z = Number.isFinite(zoom) ? zoom : 5;
  if (z <= 6) {
    return {
      name: "coarse",
      targetResDeg: 0.06,
      spacingLabel: "~6.7 km",
      windowPadDeg: 2.0,
      maxSide: 128,
    };
  }
  if (z <= 9) {
    return {
      name: "mid",
      targetResDeg: 0.015,
      spacingLabel: "~1.7 km",
      windowPadDeg: 1.0,
      maxSide: 192,
    };
  }
  if (z <= 12) {
    return {
      name: "fine",
      targetResDeg: 0.0045,
      spacingLabel: "~500 m",
      windowPadDeg: 0.5,
      maxSide: 256,
    };
  }
  return {
    name: "ultra",
    targetResDeg: 0.0018,
    spacingLabel: "~200 m",
    windowPadDeg: 0.25,
    maxSide: 320,
  };
}

/** PMTiles max zoom is 14; z15+ reuses z14. */
// source: build_coastline_tiles.py -Z0 -z14; build_depth_tiles.py -Z4 -z14
export function pmtilesZoom(zoom: number): number {
  return Math.min(14, Math.max(0, Math.floor(zoom)));
}

// source: build_depth_tiles.py _isobath_minzoom / _FILL_BAND_MINZOOM
const ISOBATH_GATES: { minzoom: number; label: string }[] = [
  { minzoom: 12, label: "≤1m" },
  { minzoom: 11, label: "≤2m" },
  { minzoom: 9, label: "≤5m" },
  { minzoom: 7, label: "≤10m" },
  { minzoom: 6, label: "≤20m" },
  { minzoom: 4, label: ">20m" },
];

const FILL_BAND_GATES: { name: string; minzoom: number }[] = [
  { name: "0–5m", minzoom: 9 },
  { name: "5–10m", minzoom: 7 },
  { name: "10–50m", minzoom: 5 },
  { name: "50m+", minzoom: 4 },
];

export function activeDepthFeatures(zoom: number): {
  isobaths: string;
  fills: string;
} {
  const z = Math.floor(zoom);
  const activeIsos = ISOBATH_GATES.filter((g) => z >= g.minzoom).map((g) => g.label);
  const activeFills = FILL_BAND_GATES.filter((g) => z >= g.minzoom).map((g) => g.name);
  return {
    isobaths: activeIsos.length ? activeIsos.join(", ") : "—",
    fills: activeFills.length ? activeFills.join(", ") : "—",
  };
}

// source: fetch_osm_chart_features.py / extract_osm_pbf_features.py
const CHART_FEATURE_GATES: { name: string; minzoom: number }[] = [
  { name: "city", minzoom: 4 },
  { name: "island", minzoom: 5 },
  { name: "town", minzoom: 6 },
  { name: "harbour", minzoom: 8 },
  { name: "motorway", minzoom: 5 },
  { name: "trunk", minzoom: 6 },
  { name: "primary", minzoom: 7 },
  { name: "water", minzoom: 6 },
  { name: "rivers", minzoom: 7 },
];

export function activeChartFeatures(zoom: number): string {
  const z = Math.floor(zoom);
  const active = CHART_FEATURE_GATES.filter((g) => z >= g.minzoom).map((g) => g.name);
  return active.length ? active.join(", ") : "land/coast only";
}

// source: MapView.tsx buildChartStyle — zoom-gated layers only (land/coast always on)
const STYLE_LAYER_GATES: { id: string; minzoom: number }[] = [
  { id: "depth-fill", minzoom: 4 },
  { id: "depth-line", minzoom: 4 },
  { id: "depth-label", minzoom: 8 },
  { id: "roads", minzoom: 5 },
  { id: "rivers", minzoom: 9 },
  { id: "place-labels", minzoom: 4 },
];

export function activeStyleLayers(zoom: number): string {
  const active = STYLE_LAYER_GATES.filter((g) => zoom >= g.minzoom).map((g) => g.id);
  return active.length ? active.join(", ") : "—";
}

/**
 * Baseline rows: what is loading at this zoom.
 * Forecast path: forcing (model-native) → terrain lib (200 m) → display grid (zoom tier).
 */
export function describeZoomDebug(zoom: number): {
  header: string;
  rows: ZoomDebugRow[];
} {
  const z = Number.isFinite(zoom) ? zoom : 5;
  const tileZ = pmtilesZoom(z);
  const tier = tierForZoom(z);
  const depth = activeDepthFeatures(z);
  const pad =
    tier.windowPadDeg === Math.floor(tier.windowPadDeg)
      ? `${tier.windowPadDeg}°`
      : `${tier.windowPadDeg}°`;

  return {
    header: `z ${z.toFixed(1)} · tile z${tileZ}`,
    rows: [
      // /wind/area display series — zoom-tiered on-wire grid
      {
        label: "Forecast",
        value: `${tier.name} · ${tier.targetResDeg}° (${tier.spacingLabel})`,
      },
      {
        label: "Grid",
        value: `pad ${pad} · ≤${tier.maxSide}² pts`,
      },
      // Terrain downscaling always at full lib res; overview_for_zoom unused
      {
        label: "Terrain",
        value: "200 m lib · overview=1 (not zoomed)",
      },
      // GRIB forcing cache — independent of map zoom
      {
        label: "Forcing",
        value: "model-native (not zoomed)",
      },
      { label: "Depth", value: `lines ${depth.isobaths}` },
      { label: "Fills", value: depth.fills },
      { label: "Chart", value: activeChartFeatures(z) },
      { label: "Style", value: activeStyleLayers(z) },
    ],
  };
}
