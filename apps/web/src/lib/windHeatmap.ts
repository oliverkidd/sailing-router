/** Windy-style smooth speed heatmap drawn to a canvas. */

import type maplibregl from "maplibre-gl";
import { windyColor } from "./colors";
import type { MeteoGrid } from "./meteoGrid";
import { sampleGrid } from "./meteoGrid";

// MapLibre renders power-of-2 square canvas sources as opaque black quads (known bug).
const RES = 255;

export class WindHeatmapLayer {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private grid: MeteoGrid | null = null;
  private map: maplibregl.Map | null = null;
  private id = "wind-heatmap-canvas";

  constructor() {
    this.canvas = document.createElement("canvas");
    this.canvas.width = RES;
    this.canvas.height = RES;
    const ctx = this.canvas.getContext("2d", { alpha: true });
    if (!ctx) throw new Error("no 2d context");
    this.ctx = ctx;
  }

  attach(map: maplibregl.Map) {
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
      map.addLayer({
        id: this.id,
        type: "raster",
        source: this.id,
        paint: { "raster-opacity": 0.5, "raster-fade-duration": 0 },
      });
    }
    this.clearCanvas();
  }

  private clearCanvas() {
    this.ctx.clearRect(0, 0, RES, RES);
    const src = this.map?.getSource(this.id) as maplibregl.CanvasSource | undefined;
    src?.play();
  }

  setGrid(grid: MeteoGrid | null) {
    this.grid = grid;
    if (!grid || !this.map) {
      this.clearCanvas();
      return;
    }
    const { lat0, lon0, dlat, dlon, nlat, nlon } = grid;
    const latMax = lat0 + dlat * (nlat - 1);
    const lonMax = lon0 + dlon * (nlon - 1);
    const src = this.map.getSource(this.id) as maplibregl.CanvasSource | undefined;
    src?.setCoordinates([
      [lon0, latMax],
      [lonMax, latMax],
      [lonMax, lat0],
      [lon0, lat0],
    ]);
    this.render();
  }

  private render() {
    const grid = this.grid;
    if (!grid) {
      this.clearCanvas();
      return;
    }
    const img = this.ctx.createImageData(RES, RES);
    const { lat0, lon0, dlat, dlon, nlat, nlon } = grid;
    const latMax = lat0 + dlat * (nlat - 1);
    const lonMax = lon0 + dlon * (nlon - 1);
    for (let py = 0; py < RES; py++) {
      for (let px = 0; px < RES; px++) {
        const lat = latMax - (py / (RES - 1)) * (latMax - lat0);
        const lon = lon0 + (px / (RES - 1)) * (lonMax - lon0);
        const { speed } = sampleGrid(grid, lat, lon);
        const col = windyColor(speed);
        const m = /rgb\((\d+),(\d+),(\d+)\)/.exec(col);
        const i = (py * RES + px) * 4;
        if (m) {
          img.data[i] = Number(m[1]);
          img.data[i + 1] = Number(m[2]);
          img.data[i + 2] = Number(m[3]);
          img.data[i + 3] = speed < 0.5 ? 0 : 180;
        }
      }
    }
    this.ctx.putImageData(img, 0, 0);
    const src = this.map?.getSource(this.id) as maplibregl.CanvasSource | undefined;
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
