/** Cyclades / DEM Terrain-RGB helpers for MapLibre setTerrain + hillshade. */

export const TERRAIN_SOURCE_ID = "terrain-dem";
export const TERRAIN_HILLSHADE_ID = "terrain-hillshade";
export const DEM_COVERAGE_SOURCE_ID = "dem-coverage";
export const DEM_COVERAGE_LAYER_ID = "dem-coverage-outline";

/** Enable 3D mesh at this zoom and above (chart still flat below). */
export const TERRAIN_MESH_MIN_ZOOM = 12;
export const TERRAIN_EXAGGERATION = 1.35;
/** Hide screen-space wind canvas when pitch exceeds this (drape mismatch). */
export const WIND_HIDE_PITCH_DEG = 2;

export interface DemBounds {
  lon_min: number;
  lat_min: number;
  lon_max: number;
  lat_max: number;
}

export function footprintsToFeatureCollection(
  footprints: Array<DemBounds & { name?: string }>,
): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: footprints.map((fp) => ({
      type: "Feature",
      properties: { name: fp.name ?? "dem" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [fp.lon_min, fp.lat_min],
            [fp.lon_max, fp.lat_min],
            [fp.lon_max, fp.lat_max],
            [fp.lon_min, fp.lat_max],
            [fp.lon_min, fp.lat_min],
          ],
        ],
      },
    })),
  };
}

export function boundsIntersect(
  a: { west: number; south: number; east: number; north: number },
  b: DemBounds,
): boolean {
  return !(a.east < b.lon_min || a.west > b.lon_max || a.north < b.lat_min || a.south > b.lat_max);
}

export function viewportIntersectsAnyDem(
  west: number,
  south: number,
  east: number,
  north: number,
  footprints: DemBounds[],
): boolean {
  const view = { west, south, east, north };
  return footprints.some((fp) => boundsIntersect(view, fp));
}

export type TerrainTileMeta = {
  minzoom: number;
  maxzoom: number;
  bounds: [number, number, number, number]; // lon_min, lat_min, lon_max, lat_max
};

/** Probe /geo/terrain/tilejson.json — null when tiles have not been built. */
export async function fetchTerrainTileMeta(origin: string): Promise<TerrainTileMeta | null> {
  try {
    const res = await fetch(`${origin}/geo/terrain/tilejson.json`, { cache: "no-store" });
    if (!res.ok) return null;
    const j = (await res.json()) as {
      minzoom?: number;
      maxzoom?: number;
      bounds?: number[];
    };
    if (!j.bounds || j.bounds.length < 4) return null;
    return {
      minzoom: j.minzoom ?? 8,
      maxzoom: j.maxzoom ?? 14,
      bounds: [j.bounds[0], j.bounds[1], j.bounds[2], j.bounds[3]],
    };
  } catch {
    return null;
  }
}
