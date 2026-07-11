/** Discrete SVG wind arrows on a fixed screen-spaced grid — no particle RAF. */

import type maplibregl from "maplibre-gl";
import type { MeteoGrid } from "./meteoGrid";
import { sampleGrid } from "./meteoGrid";

const SOURCE_ID = "wind-arrows";
const LAYER_ID = "wind-arrows";
const ICON_ID = "wind-arrow";

/** Target pixels between arrows — screen-space stride so density is zoom-stable. */
const BASE_SPACING_PX = 48;
const FADE_MS = 500;
const MIN_SPEED_KN = 0.5;

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

/** Bearing wind flows toward (degrees clockwise from north). u=east, v=north m/s. */
export function flowBearingDeg(u: number, v: number): number {
  return ((Math.atan2(u, v) * 180) / Math.PI + 360) % 360;
}

export class WindArrowLayer {
  private map: maplibregl.Map | null = null;
  private grid: MeteoGrid | null = null;
  private fadeTimer: ReturnType<typeof setTimeout> | null = null;
  private hasData = false;

  attach(map: maplibregl.Map, beforeId?: string) {
    this.map = map;
    if (!map.getSource(SOURCE_ID)) {
      map.addSource(SOURCE_ID, { type: "geojson", data: EMPTY });
      map.addLayer(
        {
          id: LAYER_ID,
          type: "symbol",
          source: SOURCE_ID,
          layout: {
            "icon-image": ICON_ID,
            "icon-size": [
              "interpolate",
              ["linear"],
              ["get", "speed"],
              0,
              0.35,
              10,
              0.45,
              25,
              0.55,
              40,
              0.65,
            ],
            "icon-rotate": ["get", "bearing"],
            "icon-rotation-alignment": "map",
            "icon-allow-overlap": true,
            "icon-ignore-placement": true,
          },
          paint: {
            "icon-opacity": 0,
            "icon-opacity-transition": { duration: FADE_MS },
          } as maplibregl.SymbolLayerSpecification["paint"],
        },
        beforeId,
      );
    }
  }

  setGrid(grid: MeteoGrid | null, opts?: { immediate?: boolean }) {
    this.grid = grid;
    const map = this.map;
    if (!map || !map.getSource(SOURCE_ID)) return;

    if (this.fadeTimer) {
      clearTimeout(this.fadeTimer);
      this.fadeTimer = null;
    }

    if (!grid) {
      this.setOpacity(0);
      (map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource).setData(EMPTY);
      this.hasData = false;
      return;
    }

    const apply = () => {
      if (!this.map || this.grid !== grid) return;
      const fc = this.buildArrows(grid);
      (this.map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource).setData(fc);
      this.hasData = true;
      requestAnimationFrame(() => this.setOpacity(0.9));
    };

    // Playback / scrubbing: swap immediately so arrows keep up with the handle.
    if (opts?.immediate) {
      apply();
      return;
    }

    // Fade out existing arrows before swapping; first paint skips the wait.
    this.setOpacity(0);
    if (this.hasData) {
      this.fadeTimer = setTimeout(apply, FADE_MS);
    } else {
      apply();
    }
  }

  private buildArrows(grid: MeteoGrid): GeoJSON.FeatureCollection {
    const map = this.map;
    if (!map) return EMPTY;

    const canvas = map.getCanvas();
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const spacing = BASE_SPACING_PX;

    const { lat0, lon0, dlat, dlon, nlat, nlon } = grid;
    const latMax = lat0 + dlat * (nlat - 1);
    const lonMax = lon0 + dlon * (nlon - 1);

    const features: GeoJSON.Feature[] = [];
    const cols = Math.max(2, Math.floor(w / spacing));
    const rows = Math.max(2, Math.floor(h / spacing));
    const stepX = w / cols;
    const stepY = h / rows;

    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < cols; col++) {
        const px = stepX * (col + 0.5);
        const py = stepY * (row + 0.5);
        const lngLat = map.unproject([px, py]);
        const lat = lngLat.lat;
        const lon = lngLat.lng;

        if (lat < lat0 || lat > latMax || lon < lon0 || lon > lonMax) continue;

        const { u, v, speed } = sampleGrid(grid, lat, lon);
        if (speed < MIN_SPEED_KN) continue;

        features.push({
          type: "Feature",
          properties: {
            bearing: Math.round(flowBearingDeg(u, v) * 10) / 10,
            speed: Math.round(speed * 10) / 10,
          },
          geometry: { type: "Point", coordinates: [lon, lat] },
        });
      }
    }

    return { type: "FeatureCollection", features };
  }

  private setOpacity(opacity: number) {
    const map = this.map;
    if (!map || !map.getLayer(LAYER_ID)) return;
    map.setPaintProperty(LAYER_ID, "icon-opacity", opacity);
  }

  setVisible(map: maplibregl.Map, visible: boolean) {
    if (!map.getLayer(LAYER_ID)) return;
    map.setLayoutProperty(LAYER_ID, "visibility", visible ? "visible" : "none");
    if (visible && this.hasData) this.setOpacity(0.9);
    else if (!visible) this.setOpacity(0);
  }

  destroy(map: maplibregl.Map) {
    if (this.fadeTimer) clearTimeout(this.fadeTimer);
    if (map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
    if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
    this.map = null;
    this.grid = null;
    this.hasData = false;
  }
}

export { ICON_ID as WIND_ARROW_ICON_ID };
