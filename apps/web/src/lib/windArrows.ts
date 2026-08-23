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

export const ARROW_SPACING_PX = BASE_SPACING_PX;
export const ARROW_PEAK_OPACITY = PEAK_OPACITY;
export const ARROW_HYBRID_SPACING_PX = 88;
export const ARROW_HYBRID_OPACITY = 0.45;
const FRAME_MS = 1000 / 30;
const EARTH_CIRCUMFERENCE_M = 40075016.686;
const METERS_PER_DEG_LAT = 111320;

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

type ArrowBase = {
  lon: number;
  lat: number;
  bearing: number;
  speed: number;
  seed: number;
  /** Degrees of lon/lat per pixel of travel along flow (valid until next rebuild). */
  dLon: number;
  dLat: number;
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
  private mapMoving = false;
  private spacingPx = BASE_SPACING_PX;
  private peakOpacity = PEAK_OPACITY;
  private onMoveStart: (() => void) | null = null;
  private onMoveEnd: (() => void) | null = null;

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
    this.bindMoveListeners(map);
  }

  private bindMoveListeners(map: maplibregl.Map) {
    this.unbindMoveListeners();
    this.onMoveStart = () => {
      this.mapMoving = true;
    };
    this.onMoveEnd = () => {
      this.mapMoving = false;
      if (this.animating && this.hasData) {
        this.lastFrame = 0;
        this.paintFrame(performance.now(), true);
      }
    };
    map.on("movestart", this.onMoveStart);
    map.on("zoomstart", this.onMoveStart);
    map.on("moveend", this.onMoveEnd);
    map.on("zoomend", this.onMoveEnd);
  }

  private unbindMoveListeners() {
    const map = this.map;
    if (!map) return;
    if (this.onMoveStart) {
      map.off("movestart", this.onMoveStart);
      map.off("zoomstart", this.onMoveStart);
    }
    if (this.onMoveEnd) {
      map.off("moveend", this.onMoveEnd);
      map.off("zoomend", this.onMoveEnd);
    }
    this.onMoveStart = null;
    this.onMoveEnd = null;
  }

  setStyle(style: { spacingPx?: number; peakOpacity?: number }) {
    this.spacingPx = style.spacingPx ?? BASE_SPACING_PX;
    this.peakOpacity = style.peakOpacity ?? PEAK_OPACITY;
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
    const t0 = performance.now();

    const canvas = map.getCanvas();
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const spacing = this.spacingPx;
    const zoomScale = 512 * 2 ** map.getZoom();

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

        const bearing = Math.round(flowBearingDeg(u, v) * 10) / 10;
        const latRad = (lat * Math.PI) / 180;
        const cosLat = Math.max(0.15, Math.cos(latRad));
        const metersPerPx = (EARTH_CIRCUMFERENCE_M * cosLat) / zoomScale;
        const rad = (bearing * Math.PI) / 180;
        const dLat = (Math.cos(rad) * metersPerPx) / METERS_PER_DEG_LAT;
        const dLon = (Math.sin(rad) * metersPerPx) / (METERS_PER_DEG_LAT * cosLat);

        bases.push({
          lon,
          lat,
          bearing,
          speed: Math.round(speed * 10) / 10,
          seed: hash01(lat, lon),
          dLon,
          dLat,
        });
      }
    }

    if (process.env.NODE_ENV === "development") {
      console.info("[wind] arrow bases", {
        count: bases.length,
        ms: Math.round(performance.now() - t0),
      });
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
    const features: GeoJSON.Feature[] = this.bases.map((b) => {
      const { cycleMs, travelPx } = windScale(b.speed);
      const { opacity, travel } = cycleEnvelope(now / cycleMs + b.seed);
      const dist = travel * travelPx;

      return {
        type: "Feature",
        properties: {
          bearing: b.bearing,
          animOpacity: Math.round(opacity * this.peakOpacity * layerFade * 1000) / 1000,
        },
        geometry: {
          type: "Point",
          coordinates: [b.lon + b.dLon * dist, b.lat + b.dLat * dist],
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
    // Skip setData while the map is panning/zooming — frees the main thread
    // at ultra zoom; resume paints a fresh frame on moveend.
    if (!this.mapMoving) {
      this.paintFrame(now);
    }
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
    this.unbindMoveListeners();
    this.stopAnim();
    if (map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
    if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
    this.map = null;
    this.grid = null;
    this.bases = [];
    this.hasData = false;
    this.mapMoving = false;
  }
}

export { ICON_ID as WIND_ARROW_ICON_ID };
