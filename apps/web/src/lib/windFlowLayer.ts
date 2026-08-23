/** GeoJSON ghost trails. MapLibre drapes line layers on terrain automatically. */

import maplibregl from "maplibre-gl";
import { sampleGrid, type MeteoGrid } from "./meteoGrid";
import { inGrid, metersPerPixel, MIN_SPEED_KN, stepFlow } from "./windFlow";

export const WIND_FLOW_PARTICLES_SOURCE_ID = "wind-flow-particles";
export const WIND_FLOW_PARTICLE_LAYER_ID = "wind-flow-particles";
export const WIND_FLOW_TAIL_LAYER_ID = "wind-flow-tails";

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
const STALE_CUSTOM_LAYER_ID = "wind-flow-elevated";

const MAX_PARTICLES = 400;
const MAX_PARTICLES_PITCHED = 250;
const HEAD_PTS = 4;
const GHOST_PTS = 22;
const PARTICLE_FRAME_MS = 1000 / 12;
const PARTICLE_STEP_PX = 3.2;
const PARTICLE_REF_MS = 8;
const MARGIN_PX = 48;

type Particle = {
  lat: number;
  lon: number;
  age: number;
  maxAge: number;
  coords: [number, number][];
};

function vis(on: boolean): "visible" | "none" {
  return on ? "visible" : "none";
}

function lifeOf(p: Particle): number {
  const fadeIn = Math.min(1, p.age / 3);
  return fadeIn * Math.max(0, 1 - p.age / Math.max(1, p.maxAge));
}

export class WindFlowLayer {
  private map: maplibregl.Map | null = null;
  private grid: MeteoGrid | null = null;
  private visible = false;
  private mapMoving = false;
  private raf: number | null = null;
  private lastFrame = 0;
  private animating = false;
  private particles: Particle[] = [];
  private onMoveStart: (() => void) | null = null;
  private onMoveEnd: (() => void) | null = null;

  attach(map: maplibregl.Map, beforeId?: string) {
    this.map = map;
    if (map.getLayer(STALE_CUSTOM_LAYER_ID)) map.removeLayer(STALE_CUSTOM_LAYER_ID);
    if (!map.getSource(WIND_FLOW_PARTICLES_SOURCE_ID)) {
      map.addSource(WIND_FLOW_PARTICLES_SOURCE_ID, {
        type: "geojson",
        data: EMPTY,
      });
      map.addLayer(
        {
          id: WIND_FLOW_TAIL_LAYER_ID,
          type: "line",
          source: WIND_FLOW_PARTICLES_SOURCE_ID,
          filter: ["==", ["get", "kind"], "tail"],
          layout: {
            "line-cap": "round",
            "line-join": "round",
            visibility: "none",
          },
          paint: {
            "line-width": 1.05,
            "line-color": "#ffffff",
            "line-opacity": ["*", 0.28, ["coalesce", ["get", "life"], 1]],
          },
        },
        beforeId,
      );
      map.addLayer(
        {
          id: WIND_FLOW_PARTICLE_LAYER_ID,
          type: "line",
          source: WIND_FLOW_PARTICLES_SOURCE_ID,
          filter: ["==", ["get", "kind"], "head"],
          layout: {
            "line-cap": "round",
            "line-join": "round",
            visibility: "none",
          },
          paint: {
            "line-color": "#ffffff",
            "line-width": 1.75,
            "line-opacity": ["*", 0.82, ["coalesce", ["get", "life"], 1]],
          },
        },
        beforeId,
      );
    }
    this.bindMoveListeners(map);
    this.syncLayerVisibility();
    this.zeroLineTransitions();
  }

  private zeroLineTransitions() {
    const map = this.map;
    if (!map) return;
    const t = { duration: 0, delay: 0 };
    for (const id of [WIND_FLOW_TAIL_LAYER_ID, WIND_FLOW_PARTICLE_LAYER_ID]) {
      if (!map.getLayer(id)) continue;
      map.setPaintProperty(id, "line-opacity-transition" as "line-opacity", t as never);
      map.setPaintProperty(id, "line-width-transition" as "line-width", t as never);
      map.setPaintProperty(id, "line-color-transition" as "line-color", t as never);
    }
  }

  private bindMoveListeners(map: maplibregl.Map) {
    this.unbindMoveListeners();
    this.onMoveStart = () => {
      this.mapMoving = true;
    };
    this.onMoveEnd = () => {
      this.mapMoving = false;
    };
    map.on("movestart", this.onMoveStart);
    map.on("moveend", this.onMoveEnd);
  }

  private unbindMoveListeners() {
    const map = this.map;
    if (!map) return;
    if (this.onMoveStart) map.off("movestart", this.onMoveStart);
    if (this.onMoveEnd) map.off("moveend", this.onMoveEnd);
    this.onMoveStart = null;
    this.onMoveEnd = null;
  }

  setGrid(grid: MeteoGrid | null) {
    if (grid === this.grid) return;
    this.grid = grid;
    if (!grid) {
      this.clearSources();
      this.stopParticles();
      return;
    }
    if (this.visible) this.rebuild();
  }

  /** Pitch/terrain no longer switches renderer. Kept so MapView can keep calling it. */
  setTerrainActive(_active: boolean) {}

  rebuild() {
    const map = this.map;
    const grid = this.grid;
    if (!map || !grid || !this.visible) return;
    this.ensureParticles(map, grid, true);
    this.startParticles();
    this.syncLayerVisibility();
  }

  setVisible(_map: maplibregl.Map, visible: boolean) {
    this.visible = visible;
    this.syncLayerVisibility();
    if (!visible) {
      this.stopParticles();
      return;
    }
    if (this.grid) this.rebuild();
  }

  private syncLayerVisibility() {
    const map = this.map;
    if (!map) return;
    const show = this.visible;
    if (map.getLayer(WIND_FLOW_TAIL_LAYER_ID)) {
      map.setLayoutProperty(WIND_FLOW_TAIL_LAYER_ID, "visibility", vis(show));
    }
    if (map.getLayer(WIND_FLOW_PARTICLE_LAYER_ID)) {
      map.setLayoutProperty(WIND_FLOW_PARTICLE_LAYER_ID, "visibility", vis(show));
    }
  }

  private setParticles(data: GeoJSON.FeatureCollection) {
    const map = this.map;
    if (!map) return;
    const src = map.getSource(WIND_FLOW_PARTICLES_SOURCE_ID) as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(data);
  }

  private clearSources() {
    this.setParticles(EMPTY);
    this.particles = [];
  }

  private particleCount(zoom: number): number {
    const pitched = (this.map?.getPitch() ?? 0) > 5;
    const cap = pitched ? MAX_PARTICLES_PITCHED : MAX_PARTICLES;
    return Math.min(cap, Math.max(280, Math.round(220 + zoom * 14)));
  }

  private inView(map: maplibregl.Map, lat: number, lon: number): boolean {
    const p = map.project([lon, lat]);
    const canvas = map.getCanvas();
    return (
      p.x > -MARGIN_PX &&
      p.x < canvas.clientWidth + MARGIN_PX &&
      p.y > -MARGIN_PX &&
      p.y < canvas.clientHeight + MARGIN_PX
    );
  }

  private spawnParticle(map: maplibregl.Map, grid: MeteoGrid, p: Particle) {
    const canvas = map.getCanvas();
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const n = GHOST_PTS;
    for (let attempt = 0; attempt < 10; attempt++) {
      const px = Math.random() * w;
      const py = Math.random() * h;
      const ll = map.unproject([px, py]);
      const lat = ll.lat;
      const lon = ll.lng;
      if (!inGrid(grid, lat, lon)) continue;
      const { speed } = sampleGrid(grid, lat, lon);
      if (speed < MIN_SPEED_KN) continue;
      p.lat = lat;
      p.lon = lon;
      p.age = 0;
      p.maxAge = 36 + Math.floor(Math.random() * 48);
      if (p.coords.length !== n) {
        p.coords = Array.from({ length: n }, () => [lon, lat] as [number, number]);
      } else {
        for (let i = 0; i < n; i++) p.coords[i] = [lon, lat];
      }
      return;
    }
    p.age = p.maxAge;
  }

  private ensureParticles(map: maplibregl.Map, grid: MeteoGrid, respawn: boolean) {
    const n = this.particleCount(map.getZoom());
    const pts = GHOST_PTS;
    const sizeChanged = this.particles.length !== n || this.particles[0]?.coords.length !== pts;
    if (sizeChanged) {
      this.particles = Array.from({ length: n }, () => ({
        lat: 0,
        lon: 0,
        age: 0,
        maxAge: 40,
        coords: Array.from({ length: pts }, () => [0, 0] as [number, number]),
      }));
      respawn = true;
    }
    if (respawn) {
      for (const p of this.particles) this.spawnParticle(map, grid, p);
    }
    this.paintParticles();
  }

  private paintParticles() {
    const features: GeoJSON.Feature[] = [];
    for (let i = 0; i < this.particles.length; i++) {
      const p = this.particles[i];
      if (!p) continue;
      const life = lifeOf(p);
      const headStart = Math.max(0, p.coords.length - HEAD_PTS);
      features.push({
        type: "Feature",
        properties: { i, kind: "head", life },
        geometry: { type: "LineString", coordinates: p.coords.slice(headStart) },
      });
      if (p.coords.length > HEAD_PTS) {
        features.push({
          type: "Feature",
          properties: { i, kind: "tail", life },
          geometry: { type: "LineString", coordinates: p.coords },
        });
      }
    }
    this.setParticles({ type: "FeatureCollection", features });
    this.syncLayerVisibility();
  }

  private advectParticles() {
    const map = this.map;
    const grid = this.grid;
    if (!map || !grid) return;
    this.ensureParticles(map, grid, false);
    const zoom = map.getZoom();
    const centerLat = map.getCenter().lat;
    const dt = (PARTICLE_STEP_PX * metersPerPixel(centerLat, zoom)) / PARTICLE_REF_MS;

    for (const p of this.particles) {
      p.age += 1;
      const next = stepFlow(grid, p.lat, p.lon, dt);
      const dead = !next || p.age > p.maxAge || !this.inView(map, next.lat, next.lon);
      if (dead) {
        this.spawnParticle(map, grid, p);
        continue;
      }
      p.lat = next.lat;
      p.lon = next.lon;
      p.coords.shift();
      p.coords.push([next.lon, next.lat]);
    }
    this.paintParticles();
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
    if (!this.mapMoving && now - this.lastFrame >= PARTICLE_FRAME_MS) {
      this.lastFrame = now;
      this.advectParticles();
    }
    this.raf = requestAnimationFrame(this.tick);
  };

  private startParticles() {
    if (this.animating) return;
    this.animating = true;
    this.lastFrame = 0;
    this.raf = requestAnimationFrame(this.tick);
  }

  private stopParticles() {
    this.animating = false;
    if (this.raf != null) {
      cancelAnimationFrame(this.raf);
      this.raf = null;
    }
  }

  destroy(map: maplibregl.Map) {
    this.unbindMoveListeners();
    this.stopParticles();
    if (map.getLayer(STALE_CUSTOM_LAYER_ID)) map.removeLayer(STALE_CUSTOM_LAYER_ID);
    for (const id of [WIND_FLOW_TAIL_LAYER_ID, WIND_FLOW_PARTICLE_LAYER_ID]) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    if (map.getSource(WIND_FLOW_PARTICLES_SOURCE_ID)) {
      map.removeSource(WIND_FLOW_PARTICLES_SOURCE_ID);
    }
    this.map = null;
    this.grid = null;
    this.particles = [];
    this.visible = false;
    this.mapMoving = false;
  }
}
