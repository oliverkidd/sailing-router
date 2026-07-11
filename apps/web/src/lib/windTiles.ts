/**
 * Windy-style LOD wind tile pyramid.
 *
 * - LOD0 overview (~0.2°) when viewport > 150 mi
 * - LOD1 local (~0.05°) when 30–150 mi
 * - LOD2 detail (model native) when < 30 mi
 *
 * Tiles are fixed 32×32, cached by model|time|lod|tileX|tileY.
 * Only missing tiles are fetched (parallel, concurrency-capped).
 */

import type { MapBounds } from "./mapOverlays";
import { fetchWindTile, sampleGrid, type MeteoGrid } from "./meteoGrid";

export const TILE_N = 32;
export const FORECAST_MAX_SPAN_MI = 300;

const LOD0_SPACING = 0.2;
const LOD1_SPACING = 0.05;
const CONCURRENCY = 5;
const MAX_CACHE = 64;

export interface TileKey {
  modelId: string;
  tUtc: string;
  lod: number;
  tileX: number;
  tileY: number;
}

export interface LodTile {
  key: string;
  bbox: MapBounds;
  grid: MeteoGrid;
}

function keyString(k: TileKey): string {
  return `${k.modelId}|${k.tUtc}|${k.lod}|${k.tileX}|${k.tileY}`;
}

/** Haversine distance in miles between two lat/lon points. */
export function haversineMiles(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 3958.8;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Max of N–S and E–W viewport spans in miles. */
export function viewportMaxSpanMiles(b: MapBounds): number {
  const midLat = (b.lat_min + b.lat_max) / 2;
  const midLon = (b.lon_min + b.lon_max) / 2;
  const ns = haversineMiles(b.lat_min, midLon, b.lat_max, midLon);
  const ew = haversineMiles(midLat, b.lon_min, midLat, b.lon_max);
  return Math.max(ns, ew);
}

export function pickLod(maxSpanMi: number): number {
  if (maxSpanMi > 150) return 0;
  if (maxSpanMi > 30) return 1;
  return 2;
}

export function lodSpacing(lod: number, modelResolutionDeg: number): number {
  if (lod === 0) return Math.max(LOD0_SPACING, modelResolutionDeg);
  if (lod === 1) return Math.max(LOD1_SPACING, modelResolutionDeg);
  return Math.max(0.01, modelResolutionDeg);
}

export function tileSizeDeg(lod: number, modelResolutionDeg: number): number {
  return TILE_N * lodSpacing(lod, modelResolutionDeg);
}

export function tilesForViewport(
  viewport: MapBounds,
  lod: number,
  modelResolutionDeg: number,
): Array<{ tileX: number; tileY: number; bbox: MapBounds }> {
  const ts = tileSizeDeg(lod, modelResolutionDeg);
  const x0 = Math.floor(viewport.lon_min / ts);
  const x1 = Math.floor((viewport.lon_max - 1e-9) / ts);
  const y0 = Math.floor(viewport.lat_min / ts);
  const y1 = Math.floor((viewport.lat_max - 1e-9) / ts);
  const out: Array<{ tileX: number; tileY: number; bbox: MapBounds }> = [];
  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      out.push({
        tileX: tx,
        tileY: ty,
        bbox: {
          lon_min: Number((tx * ts).toFixed(3)),
          lat_min: Number((ty * ts).toFixed(3)),
          lon_max: Number(((tx + 1) * ts).toFixed(3)),
          lat_max: Number(((ty + 1) * ts).toFixed(3)),
        },
      });
    }
  }
  return out;
}

/** Round time to the hour for stable cache keys. */
export function roundHourUtc(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  d.setUTCMinutes(0, 0, 0);
  return d.toISOString();
}

/** Region cell key for models/available caching (0.5°). */
export function regionCellKey(b: MapBounds): string {
  const q = (v: number) => Math.floor(v * 2) / 2;
  return `${q(b.lat_min)}_${q(b.lon_min)}_${q(b.lat_max)}_${q(b.lon_max)}`;
}

class TileCache {
  private map = new Map<string, LodTile>();
  private order: string[] = [];

  get(key: string): LodTile | undefined {
    const t = this.map.get(key);
    if (!t) return undefined;
    this.order = this.order.filter((k) => k !== key);
    this.order.push(key);
    return t;
  }

  set(tile: LodTile) {
    if (this.map.has(tile.key)) {
      this.order = this.order.filter((k) => k !== tile.key);
    }
    this.map.set(tile.key, tile);
    this.order.push(tile.key);
    while (this.order.length > MAX_CACHE) {
      const old = this.order.shift();
      if (old) this.map.delete(old);
    }
  }
}

const globalCache = new TileCache();
const inflight = new Map<string, Promise<LodTile | null>>();

async function runPool<T>(tasks: Array<() => Promise<T>>, limit: number): Promise<T[]> {
  const results: T[] = [];
  let i = 0;
  async function worker() {
    while (i < tasks.length) {
      const idx = i++;
      results[idx] = await tasks[idx]();
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, () => worker()));
  return results;
}

/**
 * Ensure all tiles covering the viewport are cached; return them.
 * Fetches only missing tiles.
 */
export async function ensureWindTiles(opts: {
  viewport: MapBounds;
  modelId: string;
  modelResolutionDeg: number;
  tUtc: string;
  signal?: AbortSignal;
}): Promise<{ tiles: LodTile[]; lod: number; error?: string }> {
  const maxSpan = viewportMaxSpanMiles(opts.viewport);
  if (maxSpan >= FORECAST_MAX_SPAN_MI) {
    return { tiles: [], lod: 0, error: "Zoom in below 300 mi to load forecast" };
  }

  const lod = pickLod(maxSpan);
  const tUtc = roundHourUtc(opts.tUtc);
  const needed = tilesForViewport(opts.viewport, lod, opts.modelResolutionDeg);
  const tiles: LodTile[] = [];
  const missing: Array<{ tileX: number; tileY: number; bbox: MapBounds; key: string }> = [];

  for (const t of needed) {
    const k: TileKey = {
      modelId: opts.modelId,
      tUtc,
      lod,
      tileX: t.tileX,
      tileY: t.tileY,
    };
    const ks = keyString(k);
    const hit = globalCache.get(ks);
    if (hit) {
      tiles.push(hit);
    } else {
      missing.push({ ...t, key: ks });
    }
  }

  if (missing.length === 0) {
    return { tiles, lod };
  }

  let lastError: string | undefined;
  const tasks = missing.map((m) => async () => {
    if (opts.signal?.aborted) return null;
    let pending = inflight.get(m.key);
    if (!pending) {
      pending = (async () => {
        try {
          const { grid, error } = await fetchWindTile(
            m.bbox,
            tUtc,
            opts.modelId,
            TILE_N,
            TILE_N,
            opts.signal,
          );
          if (!grid) {
            lastError = error;
            return null;
          }
          const tile: LodTile = { key: m.key, bbox: m.bbox, grid };
          globalCache.set(tile);
          return tile;
        } finally {
          inflight.delete(m.key);
        }
      })();
      inflight.set(m.key, pending);
    }
    return pending;
  });

  const fetched = await runPool(tasks, CONCURRENCY);
  for (const t of fetched) {
    if (t) tiles.push(t);
  }

  if (tiles.length === 0) {
    return { tiles: [], lod, error: lastError || "Wind tiles unavailable" };
  }
  return { tiles, lod, error: lastError };
}

/** Stitch LOD tiles into one regular grid covering their union (for heatmap). */
export function stitchTiles(tiles: LodTile[]): MeteoGrid | null {
  if (tiles.length === 0) return null;
  if (tiles.length === 1) return tiles[0].grid;

  const latMin = Math.min(...tiles.map((t) => t.bbox.lat_min));
  const lonMin = Math.min(...tiles.map((t) => t.bbox.lon_min));
  const latMax = Math.max(...tiles.map((t) => t.bbox.lat_max));
  const lonMax = Math.max(...tiles.map((t) => t.bbox.lon_max));

  // Use spacing from first tile
  const g0 = tiles[0].grid;
  const dlat = g0.dlat;
  const dlon = g0.dlon;
  const nlat = Math.max(2, Math.round((latMax - latMin) / dlat) + 1);
  const nlon = Math.max(2, Math.round((lonMax - lonMin) / dlon) + 1);

  // Cap stitch size for render performance
  const MAX = 96;
  const strideLat = nlat > MAX ? Math.ceil(nlat / MAX) : 1;
  const strideLon = nlon > MAX ? Math.ceil(nlon / MAX) : 1;
  const outNlat = Math.ceil(nlat / strideLat);
  const outNlon = Math.ceil(nlon / strideLon);
  const outDlat = ((nlat - 1) * dlat) / Math.max(1, outNlat - 1);
  const outDlon = ((nlon - 1) * dlon) / Math.max(1, outNlon - 1);

  const u: number[] = [];
  const v: number[] = [];
  const speed_kn: number[] = [];

  for (let i = 0; i < outNlat; i++) {
    const lat = latMin + i * outDlat;
    for (let j = 0; j < outNlon; j++) {
      const lon = lonMin + j * outDlon;
      let best: { u: number; v: number; speed: number } | null = null;
      for (const t of tiles) {
        const g = t.grid;
        const gLatMax = g.lat0 + g.dlat * (g.nlat - 1);
        const gLonMax = g.lon0 + g.dlon * (g.nlon - 1);
        if (lat < g.lat0 - 1e-6 || lat > gLatMax + 1e-6 || lon < g.lon0 - 1e-6 || lon > gLonMax + 1e-6) {
          continue;
        }
        best = sampleGrid(g, lat, lon);
        break;
      }
      if (!best) {
        u.push(0);
        v.push(0);
        speed_kn.push(0);
      } else {
        u.push(best.u);
        v.push(best.v);
        // sampleGrid returns speed in kn already derived from u/v
        speed_kn.push(best.speed);
      }
    }
  }

  return {
    lat0: latMin,
    lon0: lonMin,
    dlat: outDlat,
    dlon: outDlon,
    nlat: outNlat,
    nlon: outNlon,
    u,
    v,
    speed_kn,
    t_utc: g0.t_utc,
    model_id: g0.model_id,
    bbox: { lat_min: latMin, lon_min: lonMin, lat_max: latMax, lon_max: lonMax },
  };
}
