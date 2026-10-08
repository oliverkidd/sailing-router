/** Screen-space comet trails on a 2D overlay. GeoJSON setData cannot animate this many lines. */

import maplibregl from "maplibre-gl";
import { sampleGrid, type MeteoGrid } from "./meteoGrid";
import {
  cometLengthPx,
  FLOW_MAX_PARTICLES,
  FLOW_MAX_PARTICLES_PITCHED,
  FLOW_MIN_PARTICLES,
  FLOW_SPACING_PX,
  inGrid,
  metersPerPixel,
  MIN_SPEED_KN,
  screenParticleLayout,
  stepFlow,
  stepFlowPx,
  gridRect,
  type ScreenParticleLayout,
} from "./windFlow";
import { flowBearingDeg } from "./windArrows";

export const WIND_FLOW_PARTICLES_SOURCE_ID = "wind-flow-particles";
export const WIND_FLOW_TAIL_HALO_LAYER_ID = "wind-flow-tails-halo";
export const WIND_FLOW_TAIL_LAYER_ID = "wind-flow-tails";
export const WIND_FLOW_HEAD_LAYER_ID = "wind-flow-heads";
export const WIND_FLOW_ARROW_LAYER_ID = "wind-flow-arrows";

const FLOW_LINE_IDS = [
  WIND_FLOW_TAIL_HALO_LAYER_ID,
  WIND_FLOW_TAIL_LAYER_ID,
  WIND_FLOW_HEAD_LAYER_ID,
] as const;
const FLOW_LAYER_IDS = [...FLOW_LINE_IDS, WIND_FLOW_ARROW_LAYER_ID] as const;

const STALE_CUSTOM_LAYER_ID = "wind-flow-elevated";
const SCRATCH_PTS: { x: number; y: number }[] = Array.from({ length: 16 }, () => ({ x: 0, y: 0 }));

const GHOST_PTS = 8;
const HEAD_PTS = 2;
const SPAWN_ATTEMPTS = 24;
/** Terrain-on spawn: skip DEM unproject storms; fewer retries over land. */
const SPAWN_ATTEMPTS_MESH = 6;
/** Land/calm cells retry spawn this often instead of every frame. */
const SPAWN_RETRY_FRAMES = 16;
const TAIL_WIDTH = 1.4;
const TAIL_HALO_WIDTH = 2.2;
const HEAD_WIDTH = 2.4;
/** Paint rate. Visual speed is scaled from the original 12 fps reference. */
const PARTICLE_FRAME_MS = 1000 / 24;
const SPEED_REF_MS = 1000 / 12;
const PARTICLE_STEP_PX = 4.6;
const PARTICLE_REF_MS = 8;
const MAX_FRAME_DT_MS = 50;
const SPEED_REF_KN = 10;
/** Extra exponent on top of linear wind up through green (~15 kn). */
const SPEED_GAMMA = 1.5;
const CONTRAST_TOP_KN = 15;
const HIGH_WIND_KN = 40;
const MAX_AGE_BASE = 52;
const MAX_AGE_JITTER = 28;
const FADE_IN_FRAMES = 4;

type ScreenXy = { x: number; y: number };

type CameraTransform = {
  locationToScreenPoint?: (ll: maplibregl.LngLat, terrain?: unknown) => ScreenXy;
  locationPoint?: (ll: maplibregl.LngLat, terrain?: unknown) => ScreenXy;
  screenPointToLocation?: (p: ScreenXy, terrain?: unknown) => maplibregl.LngLat;
  pointLocation?: (p: ScreenXy, terrain?: unknown) => maplibregl.LngLat;
};

function cameraTransform(map: maplibregl.Map): CameraTransform | null {
  const raw = map as unknown as {
    transform?: CameraTransform;
    _camera?: { transform?: CameraTransform };
  };
  return raw.transform ?? raw._camera?.transform ?? null;
}

/**
 * Screen project at altitude 0. `map.project` passes the DEM and
 * getElevationForLngLat per point (~700ms for a comet field).
 */
function projectSeaLevel(map: maplibregl.Map, lon: number, lat: number): ScreenXy {
  const tr = cameraTransform(map);
  const ll = new maplibregl.LngLat(lon, lat);
  if (tr?.locationToScreenPoint) return tr.locationToScreenPoint(ll);
  if (tr?.locationPoint) return tr.locationPoint(ll);
  return map.project([lon, lat]);
}

/** Inverse of projectSeaLevel — no terrain raycast. */
function unprojectSeaLevel(map: maplibregl.Map, x: number, y: number): maplibregl.LngLat {
  const tr = cameraTransform(map);
  const pt = { x, y };
  if (tr?.screenPointToLocation) return tr.screenPointToLocation(pt);
  if (tr?.pointLocation) return tr.pointLocation(pt);
  return map.unproject([x, y]);
}

function motionFactor(speedKn: number): number {
  const kn = Math.max(speedKn, MIN_SPEED_KN);
  const atGreen = CONTRAST_TOP_KN * (CONTRAST_TOP_KN / SPEED_REF_KN) ** (SPEED_GAMMA - 1);
  const shaped =
    kn <= CONTRAST_TOP_KN
      ? kn * (kn / SPEED_REF_KN) ** (SPEED_GAMMA - 1)
      : atGreen * (1 + 0.4 * Math.min(1, (kn - CONTRAST_TOP_KN) / (HIGH_WIND_KN - CONTRAST_TOP_KN)));
  return shaped / kn;
}

type Particle = {
  lat: number;
  lon: number;
  age: number;
  maxAge: number;
  bearing: number;
  coords: [number, number][];
  /** Frames to wait before another spawn attempt after a miss. */
  rest: number;
};

function lifeOf(p: Particle): number {
  const fadeIn = Math.min(1, p.age / FADE_IN_FRAMES);
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
  private viewWest = -180;
  private viewSouth = -90;
  private viewEast = 180;
  private viewNorth = 90;
  private spreadZoom = Number.NaN;
  private dragZoom = Number.NaN;
  private latticeCols = 0;
  private latticeRows = 0;
  private overlay: HTMLCanvasElement | null = null;
  private overlayCtx: CanvasRenderingContext2D | null = null;
  private onResize: (() => void) | null = null;

  attach(map: maplibregl.Map, _beforeId?: string) {
    this.map = map;
    if (map.getLayer(STALE_CUSTOM_LAYER_ID)) map.removeLayer(STALE_CUSTOM_LAYER_ID);
    this.stripStaleGeojson(map);
    this.ensureOverlay(map);
    this.bindMoveListeners(map);
    this.syncLayerVisibility();
  }

  /** Hot reload / old sessions left GeoJSON line layers that stall the map. */
  private stripStaleGeojson(map: maplibregl.Map) {
    for (const id of FLOW_LAYER_IDS) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    if (map.getSource(WIND_FLOW_PARTICLES_SOURCE_ID)) {
      map.removeSource(WIND_FLOW_PARTICLES_SOURCE_ID);
    }
  }

  private ensureOverlay(map: maplibregl.Map) {
    if (this.onResize) {
      map.off("resize", this.onResize);
      this.onResize = null;
    }
    map.getContainer().querySelectorAll(".wind-flow-overlay").forEach((el) => el.remove());
    const canvas = document.createElement("canvas");
    canvas.className = "wind-flow-overlay";
    canvas.style.position = "absolute";
    canvas.style.inset = "0";
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.pointerEvents = "none";
    canvas.style.zIndex = "2";
    map.getContainer().appendChild(canvas);
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;
    this.overlay = canvas;
    this.overlayCtx = ctx;
    this.syncOverlaySize();
    this.onResize = () => this.syncOverlaySize();
    map.on("resize", this.onResize);
  }

  private syncOverlaySize() {
    const map = this.map;
    const canvas = this.overlay;
    const ctx = this.overlayCtx;
    if (!map || !canvas || !ctx) return;
    const gl = map.getCanvas();
    const w = Math.max(1, gl.clientWidth);
    const h = Math.max(1, gl.clientHeight);
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  private drawOverlay() {
    const map = this.map;
    const ctx = this.overlayCtx;
    const canvas = this.overlay;
    if (!map || !ctx || !canvas) return;
    const w = canvas.clientWidth || map.getCanvas().clientWidth;
    const h = canvas.clientHeight || map.getCanvas().clientHeight;
    ctx.clearRect(0, 0, w, h);
    if (!this.visible) return;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (let i = 0; i < this.particles.length; i++) {
      const p = this.particles[i];
      if (!p || p.age >= p.maxAge || p.coords.length < 2) continue;
      const life = lifeOf(p);
      if (life <= 0.02) continue;
      this.strokeComet(map, ctx, p.coords, life);
    }
    ctx.globalAlpha = 1;
  }

  private strokeComet(
    map: maplibregl.Map,
    ctx: CanvasRenderingContext2D,
    coords: [number, number][],
    life: number,
  ) {
    const n = coords.length;
    const pts = SCRATCH_PTS;
    for (let i = 0; i < n; i++) {
      const c = coords[i];
      const ll = projectSeaLevel(map, c[0], c[1]);
      pts[i].x = ll.x;
      pts[i].y = ll.y;
    }
    const last = n - 1;
    ctx.strokeStyle = "rgb(100,116,139)";
    ctx.lineWidth = TAIL_HALO_WIDTH;
    this.strokeFaded(ctx, pts, n, life);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = TAIL_WIDTH;
    this.strokeFaded(ctx, pts, n, life);
    const a = pts[Math.max(0, last - (HEAD_PTS - 1))];
    const b = pts[last];
    if (a && b && (a.x !== b.x || a.y !== b.y)) {
      ctx.globalAlpha = life;
      ctx.lineWidth = HEAD_WIDTH;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
  }

  private strokeFaded(
    ctx: CanvasRenderingContext2D,
    pts: { x: number; y: number }[],
    n: number,
    life: number,
  ) {
    if (n < 2) return;
    for (let i = 0; i < n - 1; i++) {
      const t = (i + 1) / (n - 1);
      const a = life * t * t;
      if (a < 0.02) continue;
      const p0 = pts[i];
      const p1 = pts[i + 1];
      ctx.globalAlpha = a;
      ctx.beginPath();
      ctx.moveTo(p0.x, p0.y);
      ctx.lineTo(p1.x, p1.y);
      ctx.stroke();
    }
  }

  private bindMoveListeners(map: maplibregl.Map) {
    this.unbindMoveListeners();
    this.onMoveStart = () => {
      this.mapMoving = true;
      this.dragZoom = this.map?.getZoom() ?? Number.NaN;
    };
    this.onMoveEnd = () => {
      this.mapMoving = false;
      const z = this.map?.getZoom();
      if (z != null && Number.isFinite(this.dragZoom) && Math.abs(z - this.dragZoom) >= 0.15) {
        this.spreadZoom = Number.NaN;
      }
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

  /** MapView still calls this when the DEM mesh toggles. Overlay stays visible. */
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
    if (this.visible === visible) return;
    this.visible = visible;
    this.syncLayerVisibility();
    if (!visible) {
      this.stopParticles();
      return;
    }
    if (this.grid) this.rebuild();
  }

  private syncLayerVisibility() {
    const canvas = this.overlay;
    if (!canvas) return;
    canvas.style.display = this.visible ? "block" : "none";
    if (!this.visible) this.overlayCtx?.clearRect(0, 0, canvas.width, canvas.height);
  }

  private clearSources() {
    this.particles = [];
    this.latticeCols = 0;
    this.latticeRows = 0;
    this.drawOverlay();
  }

  private particleLayout(map: maplibregl.Map): ScreenParticleLayout {
    const canvas = map.getCanvas();
    const pitched = map.getPitch() > 5;
    const cap = pitched ? FLOW_MAX_PARTICLES_PITCHED : FLOW_MAX_PARTICLES;
    return screenParticleLayout(
      canvas.clientWidth,
      canvas.clientHeight,
      FLOW_SPACING_PX,
      FLOW_MIN_PARTICLES,
      cap,
    );
  }

  private captureView(map: maplibregl.Map) {
    const b = map.getBounds();
    this.viewWest = b.getWest();
    this.viewSouth = b.getSouth();
    this.viewEast = b.getEast();
    this.viewNorth = b.getNorth();
  }

  private inView(_map: maplibregl.Map, lat: number, lon: number): boolean {
    return (
      lon >= this.viewWest &&
      lon <= this.viewEast &&
      lat >= this.viewSouth &&
      lat <= this.viewNorth
    );
  }

  private motionZoom(grid: MeteoGrid, cameraZoom: number): number {
    const g = gridRect(grid);
    const viewLat = this.viewNorth - this.viewSouth;
    const viewLon = this.viewEast - this.viewWest;
    const gridLat = Math.min(g.latMax, this.viewNorth) - Math.max(g.latMin, this.viewSouth);
    const gridLon = Math.min(g.lonMax, this.viewEast) - Math.max(g.lonMin, this.viewWest);
    if (gridLat <= 0 || gridLon <= 0 || viewLat <= 0 || viewLon <= 0) return cameraZoom;
    const cover = Math.min(gridLat / viewLat, gridLon / viewLon);
    if (cover >= 0.9) return cameraZoom;
    return cameraZoom + Math.log2(1 / Math.max(cover, 0.05));
  }

  private spawnWindow(grid: MeteoGrid): {
    south: number;
    north: number;
    west: number;
    east: number;
    ok: boolean;
  } {
    const g = gridRect(grid);
    const south = Math.max(this.viewSouth, g.latMin);
    const north = Math.min(this.viewNorth, g.latMax);
    const west = Math.max(this.viewWest, g.lonMin);
    const east = Math.min(this.viewEast, g.lonMax);
    return { south, north, west, east, ok: north > south && east > west };
  }

  private fillTail(grid: MeteoGrid, p: Particle, zoom: number) {
    const n = GHOST_PTS;
    if (p.coords.length !== n) {
      p.coords = Array.from({ length: n }, () => [p.lon, p.lat] as [number, number]);
    }
    const { u, v, speed } = sampleGrid(grid, p.lat, p.lon);
    p.bearing = flowBearingDeg(u, v);
    const { totalPx } = cometLengthPx(speed);
    const stepPx = totalPx / (n - 1);
    let lat = p.lat;
    let lon = p.lon;
    p.coords[n - 1] = [lon, lat];
    let stopped = false;
    for (let i = n - 2; i >= 0; i--) {
      if (stopped) {
        p.coords[i] = [lon, lat];
        continue;
      }
      const back = stepFlowPx(grid, lat, lon, stepPx, zoom, -1);
      if (!back) {
        stopped = true;
        p.coords[i] = [lon, lat];
        continue;
      }
      lat = back.lat;
      lon = back.lon;
      p.coords[i] = [lon, lat];
    }
  }

  private missSpawn(p: Particle) {
    p.age = p.maxAge;
    p.rest = SPAWN_RETRY_FRAMES + Math.floor(Math.random() * SPAWN_RETRY_FRAMES);
  }

  private spawnParticle(
    map: maplibregl.Map,
    grid: MeteoGrid,
    p: Particle,
    index: number,
    mode: "fade" | "spread",
  ) {
    if (!this.spawnWindow(grid).ok) {
      this.missSpawn(p);
      return;
    }
    const cols = this.latticeCols;
    const rows = this.latticeRows;
    if (cols < 1 || rows < 1) {
      this.missSpawn(p);
      return;
    }
    const col = index % cols;
    const row = Math.floor(index / cols);
    if (row >= rows) {
      this.missSpawn(p);
      return;
    }
    const canvas = map.getCanvas();
    const cellW = canvas.clientWidth / cols;
    const cellH = canvas.clientHeight / rows;
    const n = GHOST_PTS;
    const zoom = map.getZoom();
    const motionZ = this.motionZoom(grid, zoom);
    const attempts = map.getTerrain() ? SPAWN_ATTEMPTS_MESH : SPAWN_ATTEMPTS;
    for (let attempt = 0; attempt < attempts; attempt++) {
      const px = cellW * (col + Math.random());
      const py = cellH * (row + Math.random());
      const ll = unprojectSeaLevel(map, px, py);
      const lat = ll.lat;
      const lon = ll.lng;
      if (!inGrid(grid, lat, lon)) continue;
      const { speed } = sampleGrid(grid, lat, lon);
      if (speed < MIN_SPEED_KN) continue;
      p.lat = lat;
      p.lon = lon;
      p.rest = 0;
      p.maxAge = MAX_AGE_BASE + Math.floor(Math.random() * MAX_AGE_JITTER);
      p.age =
        mode === "spread"
          ? FADE_IN_FRAMES + Math.floor(Math.random() * Math.max(1, p.maxAge - FADE_IN_FRAMES - 8))
          : 0;
      if (p.coords.length !== n) {
        p.coords = Array.from({ length: n }, () => [lon, lat] as [number, number]);
      }
      this.fillTail(grid, p, motionZ);
      return;
    }
    this.missSpawn(p);
  }

  private ensureParticles(map: maplibregl.Map, grid: MeteoGrid, respawn: boolean) {
    this.captureView(map);
    const layout = this.particleLayout(map);
    const n = layout.count;
    const pts = GHOST_PTS;
    const zoom = map.getZoom();
    const latticeChanged =
      layout.cols !== this.latticeCols || layout.rows !== this.latticeRows;
    this.latticeCols = layout.cols;
    this.latticeRows = layout.rows;
    if (this.particles[0] && this.particles[0].coords.length !== pts) {
      this.particles = [];
    }
    const prevLen = this.particles.length;
    if (this.particles.length < n) {
      for (let i = this.particles.length; i < n; i++) {
        this.particles.push({
          lat: 0,
          lon: 0,
          age: MAX_AGE_BASE,
          maxAge: MAX_AGE_BASE,
          bearing: 0,
          coords: Array.from({ length: pts }, () => [0, 0] as [number, number]),
          rest: 0,
        });
      }
    } else if (this.particles.length > n) {
      this.particles.length = n;
    }
    const zoomJump = !Number.isFinite(this.spreadZoom) || Math.abs(zoom - this.spreadZoom) >= 0.2;
    const shouldSpread = respawn || latticeChanged || (!this.mapMoving && zoomJump);
    if (shouldSpread) {
      const mode = respawn || latticeChanged ? "fade" : "spread";
      for (let i = 0; i < this.particles.length; i++) {
        this.spawnParticle(map, grid, this.particles[i], i, mode);
      }
      this.spreadZoom = zoom;
    } else if (prevLen < n) {
      for (let i = prevLen; i < n; i++) {
        this.spawnParticle(map, grid, this.particles[i], i, "fade");
      }
    }
  }

  private advectParticles(elapsedMs: number) {
    const map = this.map;
    const grid = this.grid;
    if (!map || !grid) return;
    this.ensureParticles(map, grid, false);
    const zoom = map.getZoom();
    const motionZ = this.motionZoom(grid, zoom);
    const centerLat = map.getCenter().lat;
    const frameScale = Math.min(MAX_FRAME_DT_MS, elapsedMs) / SPEED_REF_MS;
    const dt =
      ((PARTICLE_STEP_PX * metersPerPixel(centerLat, motionZ)) / PARTICLE_REF_MS) * frameScale;

    for (let i = 0; i < this.particles.length; i++) {
      const p = this.particles[i];
      if (p.age >= p.maxAge) {
        if (p.rest > 0) {
          p.rest -= 1;
          continue;
        }
        this.spawnParticle(map, grid, p, i, "fade");
        continue;
      }
      p.age += 1;
      const { speed } = sampleGrid(grid, p.lat, p.lon);
      const next = stepFlow(grid, p.lat, p.lon, dt * motionFactor(speed));
      const dead = !next || p.age > p.maxAge || !this.inView(map, next.lat, next.lon);
      if (dead) {
        this.spawnParticle(map, grid, p, i, "fade");
        continue;
      }
      p.lat = next.lat;
      p.lon = next.lon;
      this.fillTail(grid, p, motionZ);
    }
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
    if (now - this.lastFrame >= PARTICLE_FRAME_MS) {
      const elapsed = this.lastFrame === 0 ? SPEED_REF_MS : now - this.lastFrame;
      this.lastFrame = now;
      this.advectParticles(elapsed);
    }
    this.drawOverlay();
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
    if (this.onResize) {
      map.off("resize", this.onResize);
      this.onResize = null;
    }
    if (this.overlay) {
      this.overlay.remove();
      this.overlay = null;
      this.overlayCtx = null;
    }
    if (map.getLayer(STALE_CUSTOM_LAYER_ID)) map.removeLayer(STALE_CUSTOM_LAYER_ID);
    this.stripStaleGeojson(map);
    this.map = null;
    this.grid = null;
    this.particles = [];
    this.visible = false;
    this.mapMoving = false;
    this.latticeCols = 0;
    this.latticeRows = 0;
  }
}
