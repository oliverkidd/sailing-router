/** Geo-referenced wind speed heatmap (canvas raster, painted once per slice). */

import type maplibregl from "maplibre-gl";
import { windScale } from "./colors";
import type { MeteoGrid } from "./meteoGrid";

// MapLibre renders power-of-2 square canvas sources as opaque black quads (known bug).
// Keep each axis odd / non-power-of-2.
const MAX_DIM = 1023;
const MIN_DIM = 63;
const UPSAMPLE_MIN = 2;
const UPSAMPLE_MAX = 6;
/** Baked layer opacity so terrain drape cannot double-apply raster-opacity. */
const PIXEL_ALPHA = 118; // 235 * 0.5
const MS_TO_KN = 1.943844;

const LUT_MAX_KN = 60;
const LUT_STEPS = LUT_MAX_KN * 10 + 1; // 0.1 kn
const COLOR_LUT = (() => {
  const lut = new Uint8Array(LUT_STEPS * 3);
  for (let i = 0; i < LUT_STEPS; i++) {
    const { rgb } = windScale(i / 10);
    lut[i * 3] = rgb[0];
    lut[i * 3 + 1] = rgb[1];
    lut[i * 3 + 2] = rgb[2];
  }
  return lut;
})();

function upsampleForGrid(nlat: number, nlon: number): number {
  const cells = Math.max(nlat, nlon, 1);
  return Math.max(UPSAMPLE_MIN, Math.min(UPSAMPLE_MAX, Math.floor(MAX_DIM / cells)));
}

function oddNonPow2(n: number): number {
  let x = Math.max(MIN_DIM, Math.min(MAX_DIM, Math.round(n)));
  if ((x & (x - 1)) === 0) x -= 1;
  if (x % 2 === 0) x -= 1;
  return Math.max(MIN_DIM, x);
}

/** Web-Mercator Y (unitless) so canvas rows match MapLibre's raster drape. */
function mercatorY(lat: number): number {
  const s = Math.min(0.9999, Math.max(-0.9999, Math.sin((lat * Math.PI) / 180)));
  return 0.5 * Math.log((1 + s) / (1 - s));
}

function latFromMercatorY(y: number): number {
  return (Math.atan(Math.sinh(y)) * 180) / Math.PI;
}

function gridExtent(grid: MeteoGrid): {
  latMin: number;
  latMax: number;
  lonMin: number;
  lonMax: number;
} {
  const lat1 = grid.lat0 + grid.dlat * (grid.nlat - 1);
  const lon1 = grid.lon0 + grid.dlon * (grid.nlon - 1);
  return {
    latMin: Math.min(grid.lat0, lat1),
    latMax: Math.max(grid.lat0, lat1),
    lonMin: Math.min(grid.lon0, lon1),
    lonMax: Math.max(grid.lon0, lon1),
  };
}

export class WindHeatmapLayer {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private grid: MeteoGrid | null = null;
  private map: maplibregl.Map | null = null;
  private id = "wind-heatmap-canvas";
  private width = MIN_DIM;
  private height = MIN_DIM;

  constructor() {
    this.canvas = document.createElement("canvas");
    this.canvas.width = this.width;
    this.canvas.height = this.height;
    const ctx = this.canvas.getContext("2d", { alpha: true });
    if (!ctx) throw new Error("no 2d context");
    this.ctx = ctx;
  }

  attach(map: maplibregl.Map, beforeId?: string) {
    this.map = map;
    if (!map.getSource(this.id)) {
      map.addSource(this.id, {
        type: "canvas",
        canvas: this.canvas,
        coordinates: [
          [-180, 85],
          [180, 85],
          [180, -85],
          [-180, -85],
        ],
        animate: false,
      });
      map.addLayer(
        {
          id: this.id,
          type: "raster",
          source: this.id,
          paint: { "raster-opacity": 1, "raster-fade-duration": 0 },
        },
        beforeId,
      );
    }
    this.clearCanvas();
  }

  /** Upload the canvas once, then freeze the source so MapLibre stops re-reading it every frame. */
  private commit() {
    const map = this.map;
    const src = map?.getSource(this.id) as maplibregl.CanvasSource | undefined;
    if (!src || !map) return;
    src.play();
    map.triggerRepaint();
    requestAnimationFrame(() => {
      try {
        src.pause();
      } catch {
        /* source may already be gone */
      }
    });
  }

  private clearCanvas() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.commit();
  }

  private resizeForGrid(grid: MeteoGrid) {
    const up = upsampleForGrid(grid.nlat, grid.nlon);
    const w = oddNonPow2(grid.nlon * up);
    const h = oddNonPow2(grid.nlat * up);
    if (w !== this.width || h !== this.height) {
      this.width = w;
      this.height = h;
      this.canvas.width = w;
      this.canvas.height = h;
    }
  }

  private setGridCoordinates(grid: MeteoGrid) {
    const map = this.map;
    if (!map) return;
    const { latMin, latMax, lonMin, lonMax } = gridExtent(grid);
    const src = map.getSource(this.id) as maplibregl.CanvasSource | undefined;
    src?.setCoordinates([
      [lonMin, latMax],
      [lonMax, latMax],
      [lonMax, latMin],
      [lonMin, latMin],
    ]);
  }

  setGrid(grid: MeteoGrid | null) {
    this.grid = grid;
    if (!grid || !this.map) {
      this.clearCanvas();
      return;
    }
    this.resizeForGrid(grid);
    this.setGridCoordinates(grid);
    this.render();
  }

  private render() {
    const grid = this.grid;
    const map = this.map;
    if (!grid || !map) {
      this.clearCanvas();
      return;
    }

    const w = this.width;
    const h = this.height;
    const { latMin, latMax, lonMin, lonMax } = gridExtent(grid);
    const lonSpan = lonMax - lonMin;
    const mercNorth = mercatorY(latMax);
    const mercSouth = mercatorY(latMin);
    const mercSpan = mercSouth - mercNorth;

    const img = this.ctx.createImageData(w, h);
    const data = img.data;
    const lut = COLOR_LUT;
    const t0 = performance.now();
    const { lat0, lon0, dlat, dlon, nlat, nlon, u, v, speed_kn } = grid;

    if (nlat < 2 || nlon < 2) {
      const speed = speed_kn[0] ?? 0;
      const idx = Math.max(0, Math.min(LUT_STEPS - 1, Math.round(speed * 10))) * 3;
      for (let i = 0; i < w * h; i++) {
        const p = i * 4;
        data[p] = lut[idx];
        data[p + 1] = lut[idx + 1];
        data[p + 2] = lut[idx + 2];
        data[p + 3] = PIXEL_ALPHA;
      }
    } else {
      const nlonMax = nlon - 1.001;
      const nlatMax = nlat - 1.001;
      const j0 = new Int32Array(w);
      const j1 = new Int32Array(w);
      const fx = new Float64Array(w);
      for (let px = 0; px < w; px++) {
        const lon = lonMin + ((px + 0.5) / w) * lonSpan;
        const fj = Math.max(0, Math.min(nlonMax, (lon - lon0) / dlon));
        const ja = Math.floor(fj);
        j0[px] = ja;
        j1[px] = Math.min(ja + 1, nlon - 1);
        fx[px] = fj - ja;
      }

      for (let py = 0; py < h; py++) {
        const lat = latFromMercatorY(mercNorth + ((py + 0.5) / h) * mercSpan);
        const fi = Math.max(0, Math.min(nlatMax, (lat - lat0) / dlat));
        const i0 = Math.floor(fi);
        const i1 = Math.min(i0 + 1, nlat - 1);
        const fy = fi - i0;
        const omfy = 1 - fy;
        const row0 = i0 * nlon;
        const row1 = i1 * nlon;
        const outRow = py * w;
        for (let px = 0; px < w; px++) {
          const f = fx[px];
          const omf = 1 - f;
          const ja = j0[px];
          const jb = j1[px];
          const su =
            omfy * (omf * (u[row0 + ja] ?? 0) + f * (u[row0 + jb] ?? 0)) +
            fy * (omf * (u[row1 + ja] ?? 0) + f * (u[row1 + jb] ?? 0));
          const sv =
            omfy * (omf * (v[row0 + ja] ?? 0) + f * (v[row0 + jb] ?? 0)) +
            fy * (omf * (v[row1 + ja] ?? 0) + f * (v[row1 + jb] ?? 0));
          const speed = Math.hypot(su, sv) * MS_TO_KN;
          const idx = Math.max(0, Math.min(LUT_STEPS - 1, Math.round(speed * 10))) * 3;
          const i = (outRow + px) * 4;
          data[i] = lut[idx];
          data[i + 1] = lut[idx + 1];
          data[i + 2] = lut[idx + 2];
          data[i + 3] = PIXEL_ALPHA;
        }
      }
    }

    this.ctx.putImageData(img, 0, 0);
    this.commit();
    if (process.env.NODE_ENV === "development") {
      console.info("[wind] heatmap paint", {
        w,
        h,
        ms: Math.round(performance.now() - t0),
      });
    }
  }

  setVisible(map: maplibregl.Map, visible: boolean) {
    if (!map.getLayer(this.id)) return;
    map.setLayoutProperty(this.id, "visibility", visible ? "visible" : "none");
  }

  /** Terrain drape uses the same raster-opacity as 2D; PIXEL_ALPHA is the only fade. */
  setDraped(map: maplibregl.Map, _draped: boolean) {
    if (!map.getLayer(this.id)) return;
    map.setPaintProperty(this.id, "raster-opacity", 1);
  }

  destroy(map: maplibregl.Map) {
    this.clearCanvas();
    if (map.getLayer(this.id)) map.removeLayer(this.id);
    if (map.getSource(this.id)) map.removeSource(this.id);
  }
}
