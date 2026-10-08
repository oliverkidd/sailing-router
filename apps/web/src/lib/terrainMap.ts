/** DEM Terrain-RGB helpers for MapLibre setTerrain + baked hillshade. */

import maplibregl from "maplibre-gl";
import type { Map as MapLibreMap } from "maplibre-gl";

export const TERRAIN_SOURCE_ID = "terrain-dem";
export const TERRAIN_SHADE_SOURCE_ID = "terrain-shade";
export const TERRAIN_SHADE_LAYER_ID = "terrain-shade";

/** Enable 3D mesh at this zoom and above (chart still flat below). */
export const TERRAIN_MESH_MIN_ZOOM = 12;
export const TERRAIN_EXAGGERATION = 1.75;

export interface DemBounds {
  lon_min: number;
  lat_min: number;
  lon_max: number;
  lat_max: number;
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
  version: string;
  shade: boolean;
};

function tileQuery(version: string): string {
  return version ? `?v=${encodeURIComponent(version)}` : "";
}

const MWTILE_PROTO = "mwtile";
let mwTileProtocolOrigin: string | null = null;

/** 256×256 white, alpha 1 — MapLibre paints fully-transparent rasters magenta. */
const EMPTY_SHADE_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAAACW0lEQVR42u3UMQEAAAjDMMC/52EAByQSerSTdAEvjQRgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGAAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGAAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYAGAAgAEABgAYABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAYACAAQAGABgAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABgAIABAAYAGABwWekCBP7ReLZJAAAAAElFTkSuQmCC";

function pngFromB64(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function ensureMwTileProtocol(origin: string): void {
  if (mwTileProtocolOrigin === origin) return;
  if (mwTileProtocolOrigin != null) {
    try {
      maplibregl.removeProtocol(MWTILE_PROTO);
    } catch {
      /* already gone */
    }
  }
  const emptyShade = pngFromB64(EMPTY_SHADE_B64);
  try {
    maplibregl.addProtocol(MWTILE_PROTO, async (params, abortController) => {
      const rest = params.url.replace(/^mwtile:\/\//, "");
      const httpUrl = `${origin.replace(/\/$/, "")}/${rest}`;
      const isShade = rest.includes("/shade/");
      try {
        const res = await fetch(httpUrl, { signal: abortController.signal });
        if (res.ok) return { data: await res.arrayBuffer() };
        // A flat 0 m stand-in makes MapLibre treat the hole as loaded terrain,
        // so the mesh drops to sea level beside real tiles. Status 404 lets it
        // keep the parent zoom's elevation instead (no console error event).
        if (!isShade) {
          const missing = new Error(`terrain tile ${res.status}`) as Error & { status: number };
          missing.status = res.status;
          throw missing;
        }
      } catch (err) {
        if (abortController.signal.aborted || !isShade) throw err;
      }
      return { data: emptyShade };
    });
  } catch {
    /* HMR: protocol already registered */
  }
  mwTileProtocolOrigin = origin;
}

/** Probe /geo/terrain/tilejson.json — null when tiles have not been built. */
export async function fetchTerrainTileMeta(origin: string): Promise<TerrainTileMeta | null> {
  try {
    const res = await fetch(`${origin}/geo/terrain/tilejson.json`, { cache: "no-store" });
    if (!res.ok) return null;
    const j = (await res.json()) as {
      minzoom?: number;
      maxzoom?: number;
      bounds?: number[];
      version?: string;
      shade?: boolean;
      shadeTiles?: unknown;
    };
    if (!j.bounds || j.bounds.length < 4) return null;
    return {
      minzoom: j.minzoom ?? 8,
      maxzoom: j.maxzoom ?? 16,
      bounds: [j.bounds[0], j.bounds[1], j.bounds[2], j.bounds[3]],
      version: j.version ?? "",
      shade: j.shade === true || Array.isArray(j.shadeTiles),
    };
  } catch {
    return null;
  }
}

/**
 * Terrain-RGB for pitched 3D mesh, plus a baked hillshade raster for top-down.
 * Shade sits under water-fill / rivers. Sea pixels are clear, so depth
 * contours stay visible where there is no relief.
 */
export function attachTerrainLayers(
  map: MapLibreMap,
  origin: string,
  terrainMeta: TerrainTileMeta,
): void {
  const q = tileQuery(terrainMeta.version);
  ensureMwTileProtocol(origin);
  const demUrl = `${MWTILE_PROTO}://geo/terrain/{z}/{x}/{y}.png${q}`;
  if (!map.getSource(TERRAIN_SOURCE_ID)) {
    map.addSource(TERRAIN_SOURCE_ID, {
      type: "raster-dem",
      tiles: [demUrl],
      tileSize: 256,
      minzoom: terrainMeta.minzoom,
      maxzoom: terrainMeta.maxzoom,
      encoding: "mapbox",
      bounds: terrainMeta.bounds,
    });
  }

  if (!terrainMeta.shade) return;

  const shadeUrl = `${MWTILE_PROTO}://geo/terrain/shade/{z}/{x}/{y}.png${q}`;
  if (!map.getSource(TERRAIN_SHADE_SOURCE_ID)) {
    map.addSource(TERRAIN_SHADE_SOURCE_ID, {
      type: "raster",
      tiles: [shadeUrl],
      tileSize: 256,
      minzoom: terrainMeta.minzoom,
      maxzoom: terrainMeta.maxzoom,
      bounds: terrainMeta.bounds,
    });
  }

  const beforeWater = map.getLayer("water-fill")
    ? "water-fill"
    : map.getLayer("coastline")
      ? "coastline"
      : undefined;

  if (!map.getLayer(TERRAIN_SHADE_LAYER_ID)) {
    map.addLayer(
      {
        id: TERRAIN_SHADE_LAYER_ID,
        type: "raster",
        source: TERRAIN_SHADE_SOURCE_ID,
        paint: {
          "raster-opacity": 1,
          "raster-resampling": "linear",
          "raster-fade-duration": 0,
        },
      },
      beforeWater,
    );
  }
}
