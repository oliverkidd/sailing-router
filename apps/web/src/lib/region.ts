/** UK + Europe chart region — matches tile builds (REGION_BBOX). */
export const REGION_BBOX = {
  lon_min: -25,
  lat_min: 30,
  lon_max: 42,
  lat_max: 72,
} as const;

/** MapLibre maxBounds: [[west, south], [east, north]]. */
export const REGION_MAX_BOUNDS: [[number, number], [number, number]] = [
  [REGION_BBOX.lon_min, REGION_BBOX.lat_min],
  [REGION_BBOX.lon_max, REGION_BBOX.lat_max],
];

/** Default map center inside the charted region. */
export const REGION_CENTER: [number, number] = [12.5, 42.0];
