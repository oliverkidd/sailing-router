/** Discrete SVG wind arrows on a screen grid — motion & density from TWS. */

import type maplibregl from "maplibre-gl";
import { windScale } from "./colors";
import type { MeteoGrid } from "./meteoGrid";
import { sampleGrid } from "./meteoGrid";

const SOURCE_ID = "wind-arrows";
const LAYER_ID = "wind-arrows";
const ICON_ID = "wind-arrow";

/** Finest screen spacing (full density at 50 kn). Lighter wind thins via keepProb. */
const BASE_SPACING_PX = 44;
const ICON_SIZE = 0.75;
const FADE_MS = 500;
const MIN_SPEED_KN = 0.5;
const PEAK_OPACITY = 0.88;
const FRAME_MS = 1000 / 30;

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

type ArrowBase = {
  lon: number;
  lat: number;
  bearing: number;
  speed: number;
  seed: number;
};

/** Bearing wind flows toward (degrees clockwise from north). u=east, v=north m/s. */
export function flowBearingDeg(u: number, v: number): number {
  return ((Math.atan2(u, v) * 180) / Math.PI + 360) % 360;
}

/** Soft fade envelope; travel progress stays linear with cycle phase. */
function cycleEnvelope(t: number): { opacity: number; travel: number } {
  const p = ((t % 1) + 1) % 1;
  let opacity: number;
  if (p < 0.18) opacity = p / 0.18;
  else if (p < 0.62) opacity = 1;
  else opacity = (1 - p) / 0.38;
  return { opacity, travel: p };
}

function hash01(a: number, b: number): number {
  const x = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return x - Math.floor(x);
}

export class WindArrowLayer {
  private map: maplibregl.Map | null = null;
  private grid: MeteoGrid | null = null;
  private fadeTimer: ReturnType<typeof setTimeout> | null = null;
  private hasData = false;
  private layerFade = 1;
  private fadeFrom = 1;
  private fadeTarget = 1;
  private fadeStart = 0;
  private bases: ArrowBase[] = [];
  private raf: number | null = null;
  private lastFrame = 0;
  private animating = false;

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
            "icon-size": ICON_SIZE,
            "icon-rotate": ["get", "bearing"],
            "icon-rotation-alignment": "map",
            "icon-allow-overlap": true,
            "icon-ignore-placement": true,
          },
          paint: {
            "icon-opacity": ["get", "animOpacity"],
            "icon-opacity-transition": { duration: 0 },
          } as maplibregl.SymbolLayerSpecification["paint"],
        },
        beforeId,
      );
    } else if (map.getLayer(LAYER_ID)) {
      map.setLayoutProperty(LAYER_ID, "icon-size", ICON_SIZE);
    }
  }

  setGrid(grid: MeteoGrid | null, opts?: { immediate?: boolean }) {
    this.grid = grid;
    const map = this.map;
    if (!map || !map.getSource(SOURCE_ID)) return;
    if (map.getLayer(LAYER_ID)) {
      map.setLayoutProperty(LAYER_ID, "icon-size", ICON_SIZE);
    }

    if (this.fadeTimer) {
      clearTimeout(this.fadeTimer);
      this.fadeTimer = null;
    }

    if (!grid) {
      this.stopAnim();
      this.snapLayerFade(0);
      (map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource).setData(EMPTY);
      this.bases = [];
      this.hasData = false;
      return;
    }

    const apply = () => {
      if (!this.map || this.grid !== grid) return;
      this.bases = this.buildBases(grid);
      this.hasData = true;
      this.paintFrame(performance.now(), true);
      this.startAnim();
      this.tweenLayerFade(1);
    };

    if (opts?.immediate) {
      this.snapLayerFade(1);
      apply();
      return;
    }

    this.tweenLayerFade(0);
    if (this.hasData) {
      this.fadeTimer = setTimeout(apply, FADE_MS);
    } else {
      apply();
    }
  }

  private buildBases(grid: MeteoGrid): ArrowBase[] {
    const map = this.map;
    if (!map) return [];

    const canvas = map.getCanvas();
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const spacing = BASE_SPACING_PX;

    const { lat0, lon0, dlat, dlon, nlat, nlon } = grid;
    const latMax = lat0 + dlat * (nlat - 1);
    const lonMax = lon0 + dlon * (nlon - 1);

    const bases: ArrowBase[] = [];
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

        const { keepProb } = windScale(speed);
        if (hash01(lat + 17.13, lon - 3.71) >= keepProb) continue;

        bases.push({
          lon,
          lat,
          bearing: Math.round(flowBearingDeg(u, v) * 10) / 10,
          speed: Math.round(speed * 10) / 10,
          seed: hash01(lat, lon),
        });
      }
    }

    return bases;
  }

  private snapLayerFade(fade: number) {
    this.layerFade = fade;
    this.fadeFrom = fade;
    this.fadeTarget = fade;
    this.fadeStart = performance.now();
  }

  private tweenLayerFade(target: number) {
    this.fadeFrom = this.layerFade;
    this.fadeTarget = target;
    this.fadeStart = performance.now();
    if (this.hasData) this.startAnim();
  }

  private updateLayerFade(now: number) {
    if (this.layerFade === this.fadeTarget) return;
    const u = Math.min(1, (now - this.fadeStart) / FADE_MS);
    this.layerFade = this.fadeFrom + (this.fadeTarget - this.fadeFrom) * u;
    if (u >= 1) this.layerFade = this.fadeTarget;
  }

  private paintFrame(now: number, force = false) {
    const map = this.map;
    if (!map || !map.getSource(SOURCE_ID) || this.bases.length === 0) return;

    this.updateLayerFade(now);
    if (!force && now - this.lastFrame < FRAME_MS) return;
    this.lastFrame = now;

    const layerFade = this.layerFade;
    const mapBearing = map.getBearing();
    const features: GeoJSON.Feature[] = this.bases.map((b) => {
      const { cycleMs, travelPx } = windScale(b.speed);
      const { opacity, travel } = cycleEnvelope(now / cycleMs + b.seed);
      const dist = travel * travelPx;
      const rad = ((b.bearing - mapBearing) * Math.PI) / 180;
      const origin = map.project([b.lon, b.lat]);
      const drifted = map.unproject([
        origin.x + Math.sin(rad) * dist,
        origin.y - Math.cos(rad) * dist,
      ]);

      return {
        type: "Feature",
        properties: {
          bearing: b.bearing,
          animOpacity: Math.round(opacity * PEAK_OPACITY * layerFade * 1000) / 1000,
        },
        geometry: {
          type: "Point",
          coordinates: [drifted.lng, drifted.lat],
        },
      };
    });

    (map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features,
    });
  }

  private tick = (now: number) => {
    this.raf = null;
    if (!this.animating) return;
    if (typeof document !== "undefined" && document.hidden) {
      const onVis = () => {
        document.removeEventListener("visibilitychange", onVis);
        if (this.animating) this.raf = requestAnimationFrame(this.tick);
      };
      document.addEventListener("visibilitychange", onVis);
      return;
    }
    this.paintFrame(now);
    this.raf = requestAnimationFrame(this.tick);
  };

  private startAnim() {
    if (this.animating) return;
    this.animating = true;
    this.lastFrame = 0;
    this.raf = requestAnimationFrame(this.tick);
  }

  private stopAnim() {
    this.animating = false;
    if (this.raf != null) {
      cancelAnimationFrame(this.raf);
      this.raf = null;
    }
  }

  setVisible(map: maplibregl.Map, visible: boolean) {
    if (!map.getLayer(LAYER_ID)) return;
    map.setLayoutProperty(LAYER_ID, "visibility", visible ? "visible" : "none");
    if (visible && this.hasData) {
      this.tweenLayerFade(1);
      this.startAnim();
    } else {
      this.snapLayerFade(0);
      this.stopAnim();
    }
  }

  destroy(map: maplibregl.Map) {
    if (this.fadeTimer) clearTimeout(this.fadeTimer);
    this.stopAnim();
    if (map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
    if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
    this.map = null;
    this.grid = null;
    this.bases = [];
    this.hasData = false;
  }
}

export { ICON_ID as WIND_ARROW_ICON_ID };
