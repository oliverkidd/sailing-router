import { PMTiles } from "pmtiles";
import { chartArchiveUrl } from "@/lib/chartUrls";
import { pinChartCache } from "@/lib/deviceCache";
import type { Course } from "@/lib/types";

const MAX_TILES = 280;

function lonToTileX(lon: number, z: number): number {
  return Math.floor(((lon + 180) / 360) * 2 ** z);
}

function latToTileY(lat: number, z: number): number {
  const clamped = Math.min(85, Math.max(-85, lat));
  const rad = (clamped * Math.PI) / 180;
  const n = Math.log(Math.tan(rad) + 1 / Math.cos(rad));
  return Math.floor(((1 - n / Math.PI) / 2) * 2 ** z);
}

/** XYZ tiles covering a lat/lon box at one zoom. */
export function tilesForBbox(
  west: number,
  south: number,
  east: number,
  north: number,
  z: number,
): Array<{ z: number; x: number; y: number }> {
  const x0 = lonToTileX(west, z);
  const x1 = lonToTileX(east, z);
  const y0 = latToTileY(north, z);
  const y1 = latToTileY(south, z);
  const out: Array<{ z: number; x: number; y: number }> = [];
  for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++) {
    for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++) {
      out.push({ z, x, y });
    }
  }
  return out;
}

export interface VoyageProgress {
  done: number;
  total: number;
  label: string;
}

/**
 * Pull the chart tiles under the course into the device cache and pin them.
 * Zooms 9 and 11 cover the race area without downloading the whole archive.
 * Stops at MAX_TILES so a long course cannot fill the phone on its own.
 */
export async function downloadVoyageChart(
  _origin: string,
  course: Course,
  onProgress?: (progress: VoyageProgress) => void,
): Promise<void> {
  const lats = [course.start[0], course.finish[0], ...course.marks.map((m) => m.position[0])];
  const lons = [course.start[1], course.finish[1], ...course.marks.map((m) => m.position[1])];
  const margin = 0.4;
  const west = Math.min(...lons) - margin;
  const east = Math.max(...lons) + margin;
  const south = Math.min(...lats) - margin;
  const north = Math.max(...lats) + margin;
  const tiles = [...tilesForBbox(west, south, east, north, 9), ...tilesForBbox(west, south, east, north, 11)];
  const limited = tiles.slice(0, MAX_TILES);
  const archives = ["coastline.pmtiles", "depth.pmtiles"] as const;
  const total = limited.length * archives.length;
  let done = 0;
  for (const name of archives) {
    const archive = new PMTiles(chartArchiveUrl(name));
    for (const tile of limited) {
      done += 1;
      onProgress?.({ done, total, label: name });
      try {
        await archive.getZxy(tile.z, tile.x, tile.y);
      } catch {
        /* Missing tile or offline: keep what we already have. */
      }
    }
  }
  await pinChartCache();
}
