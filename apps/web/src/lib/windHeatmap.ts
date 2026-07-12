/** Viewport-aligned wind speed heatmap (canvas raster). */

import type maplibregl from "maplibre-gl";
import { windScale } from "./colors";
import type { MeteoGrid } from "./meteoGrid";
import { sampleGrid } from "./meteoGrid";

// MapLibre renders power-of-2 square canvas sources as opaque black quads (known bug).
// Keep each axis odd / non-power-of-2.
const MAX_DIM = 1023;
const MIN_DIM = 63;

function oddNonPow2(n: number): number {
  let x = Math.max(MIN_DIM, Math.min(MAX_DIM, Math.round(n)));
  if ((x & (x - 1)) === 0) x -= 1;
  if (x % 2 === 0) x -= 1;
  return Math.max(MIN_DIM, x);
}

function inGrid(grid: MeteoGrid, lat: number, lon: number): boolean {
  const { lat0, lon0, dlat, dlon, nlat, nlon } = grid;
  const latMax = lat0 + dlat * (nlat - 1);
  const lonMax = lon0 + dlon * (nlon - 1);
  return lat >= lat0 && lat <= latMax && lon >= lon0 && lon <= lonMax;
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
          paint: { "raster-opacity": 0.5, "raster-fade-duration": 0 },
        },
        beforeId,
      );
    }
    this.clearCanvas();
  }

  private clearCanvas() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    const src = this.map?.getSource(this.id) as maplibregl.CanvasSource | undefined;
    src?.play();
  }

  /** Size canvas to the map's CSS pixel footprint (odd non-pow2 per axis). */
  private resizeToMap() {
    const map = this.map;
    if (!map) return;
    const cssW = map.getContainer().clientWidth;
    const cssH = map.getContainer().clientHeight;
    const w = oddNonPow2(cssW);
    const h = oddNonPow2(cssH);
    if (w !== this.width || h !== this.height) {
      this.width = w;
      this.height = h;
      this.canvas.width = w;
      this.canvas.height = h;
    }
  }

  private setViewportCoordinates() {
    const map = this.map;
    if (!map) return;
    const b = map.getBounds();
    const west = b.getWest();
    const south = b.getSouth();
    const east = b.getEast();
    const north = b.getNorth();
    const src = map.getSource(this.id) as maplibregl.CanvasSource | undefined;
    src?.setCoordinates([
      [west, north],
      [east, north],
      [east, south],
      [west, south],
    ]);
  }

  setGrid(grid: MeteoGrid | null) {
    this.grid = grid;
    if (!grid || !this.map) {
      this.clearCanvas();
      return;
    }
    this.resizeToMap();
    this.setViewportCoordinates();
    this.render();
  }

  /** Re-paint after pan/zoom so every screen pixel matches click sampling. */
  syncViewport() {
    if (!this.grid || !this.map) return;
    this.resizeToMap();
    this.setViewportCoordinates();
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
    const cssW = map.getContainer().clientWidth;
    const cssH = map.getContainer().clientHeight;
    const sx = cssW / w;
    const sy = cssH / h;

    const img = this.ctx.createImageData(w, h);
    const data = img.data;

    for (let py = 0; py < h; py++) {
      for (let px = 0; px < w; px++) {
        const i = (py * w + px) * 4;
        const lngLat = map.unproject([(px + 0.5) * sx, (py + 0.5) * sy]);
        const lat = lngLat.lat;
        const lon = lngLat.lng;

        if (!inGrid(grid, lat, lon)) {
          data[i + 3] = 0;
          continue;
        }

        const { speed } = sampleGrid(grid, lat, lon);
        const { rgb } = windScale(speed);
        data[i] = rgb[0];
        data[i + 1] = rgb[1];
        data[i + 2] = rgb[2];
        data[i + 3] = 235;
      }
    }

    this.ctx.putImageData(img, 0, 0);
    const src = map.getSource(this.id) as maplibregl.CanvasSource | undefined;
    src?.play();
  }

  setVisible(map: maplibregl.Map, visible: boolean) {
    if (!map.getLayer(this.id)) return;
    map.setLayoutProperty(this.id, "visibility", visible ? "visible" : "none");
    if (!visible) this.clearCanvas();
  }

  destroy(map: maplibregl.Map) {
    this.clearCanvas();
    if (map.getLayer(this.id)) map.removeLayer(this.id);
    if (map.getSource(this.id)) map.removeSource(this.id);
  }
}
