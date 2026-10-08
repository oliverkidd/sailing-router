"use client";

import maplibregl from "maplibre-gl";
import { Protocol } from "pmtiles";
import { useEffect, useRef } from "react";
import { api } from "@/lib/api";
import { chartPmtilesUrl } from "@/lib/chartUrls";
import { installDeviceCache } from "@/lib/deviceCache";
import {
  boundsFromCourseAndRoutes,
  boundsFromMap,
  isPointSet,
  seekTimeAlongRoute,
  type MapBounds,
} from "@/lib/mapOverlays";
import { bboxContains } from "@/lib/windTiles";
import { sampleGrid, sliceSeries, type MeteoGrid, type WindSeries } from "@/lib/meteoGrid";
import { WindHeatmapLayer } from "@/lib/windHeatmap";
import {
  WindArrowLayer,
  WIND_ARROW_ICON_ID,
} from "@/lib/windArrows";
import {
  WindFlowLayer,
  WIND_FLOW_ARROW_LAYER_ID,
  WIND_FLOW_TAIL_HALO_LAYER_ID,
  WIND_FLOW_TAIL_LAYER_ID,
} from "@/lib/windFlowLayer";
import { colorForModel } from "@/lib/colors";
import { BOAT_SVG, WIND_ARROW_SVG, svgToImage } from "@/lib/mapIcons";
import { formatPointPopup, type PointInspect } from "@/lib/mapPopup";
import { REGION_CENTER, REGION_MAX_BOUNDS } from "@/lib/region";
import { viewportMaxSpanMiles } from "@/lib/windTiles";
import type { Course, ModelRoute } from "@/lib/types";
import type { EditMode } from "@/components/CourseEditor";
import {
  TERRAIN_EXAGGERATION,
  TERRAIN_MESH_MIN_ZOOM,
  TERRAIN_SHADE_LAYER_ID,
  TERRAIN_SOURCE_ID,
  attachTerrainLayers,
  fetchTerrainTileMeta,
  viewportIntersectsAnyDem,
  type DemBounds,
} from "@/lib/terrainMap";

const DEFAULT_ZOOM = 5;
const MIN_ZOOM = 4;
/** Cap for framing a course/route — allows zoom-in from the wide default view. */
const FIT_MAX_ZOOM = 14;
/** Allow bearing rotate only once the camera is pitched into 3D. */
const ROTATE_ENABLE_PITCH_DEG = 5;

/** True when `box` is on screen and fills enough of the view (not a speck at sea). */
function bboxMeaningfullyFramed(view: MapBounds, box: MapBounds): boolean {
  if (!bboxContains(view, box, 0)) return false;
  const vLat = Math.max(1e-9, view.lat_max - view.lat_min);
  const vLon = Math.max(1e-9, view.lon_max - view.lon_min);
  const bLat = Math.max(0, box.lat_max - box.lat_min);
  const bLon = Math.max(0, box.lon_max - box.lon_min);
  return Math.max(bLat / vLat, bLon / vLon) >= 0.15;
}

/** Rough camera so the first frame is near the course before fitBounds refines it. */
function cameraForBounds(b: MapBounds): { center: [number, number]; zoom: number } {
  const center: [number, number] = [
    (b.lon_min + b.lon_max) / 2,
    (b.lat_min + b.lat_max) / 2,
  ];
  const latSpan = Math.max(0.02, b.lat_max - b.lat_min);
  const lonSpan = Math.max(0.02, b.lon_max - b.lon_min);
  const span = Math.max(latSpan, lonSpan * Math.cos((center[1] * Math.PI) / 180));
  const zoom = Math.min(
    FIT_MAX_ZOOM,
    Math.max(MIN_ZOOM, Math.round(8.2 - Math.log2(span))),
  );
  return { center, zoom };
}
/** Enable 3D terrain mesh only when pitched (top-down stays in the flat pipeline). */
const MESH_PITCH_MIN_DEG = 5;

function courseCursorCss(mode: EditMode): string {
  if (mode === "none") return "";
  const fill =
    mode === "start" ? "#4ade80" : mode === "finish" ? "#fb7185" : "#1e3a5f";
  const label = mode === "start" ? "S" : mode === "finish" ? "F" : "+";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="9" fill="${fill}" stroke="#0b1220" stroke-width="2"/>
    <text x="12" y="16" text-anchor="middle" font-size="11" font-weight="700" font-family="sans-serif" fill="${mode === "mark" ? "#ffffff" : "#0b1220"}">${label}</text>
  </svg>`;
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  return `url("${url}") 12 12, crosshair`;
}

let pmtilesRegistered = false;
export function ensurePmtilesProtocol() {
  if (pmtilesRegistered) return;
  installDeviceCache();
  const protocol = new Protocol();
  // MapLibre 5 wants a Promise loader; pmtiles.tilev4 is that shape.
  maplibregl.addProtocol("pmtiles", (params, abortController) =>
    protocol.tilev4(params, abortController),
  );
  pmtilesRegistered = true;
}

/** Dark nautical chart — land + depth isobaths from local PMTiles (no runtime fetch). */

/** Land fill (grey) + a darker stroke for a clean coastline outline. */
const LAND_FILL_COLOR = "#c8c8c8";
const COASTLINE_COLOR = "#7a7a7a";

/** Uniform isobath stroke — slate, low opacity. */
const DEPTH_LINE_COLOR = "#475569";
const DEPTH_FILL_OPACITY = 0.7;
const DEPTH_LINE_OPACITY = 0.17;
const DEPTH_TEXT_OPACITY = 0.9;

/** Hide via opacity (not visibility:none) so MapLibre keeps the source tile cache warm. */
function setDepthOpacity(map: maplibregl.Map, visible: boolean) {
  if (map.getLayer("depth-fill")) {
    map.setPaintProperty("depth-fill", "fill-opacity", visible ? DEPTH_FILL_OPACITY : 0);
  }
  if (map.getLayer("depth-line")) {
    map.setPaintProperty("depth-line", "line-opacity", visible ? DEPTH_LINE_OPACITY : 0);
  }
  if (map.getLayer("depth-label")) {
    map.setPaintProperty("depth-label", "text-opacity", visible ? DEPTH_TEXT_OPACITY : 0);
  }
}

const DEPTH_LINE_PAINT: maplibregl.LineLayerSpecification["paint"] = {
  "line-color": DEPTH_LINE_COLOR,
  "line-width": [
    "interpolate",
    ["linear"],
    ["zoom"],
    4,
    0.4,
    10,
    0.7,
    14,
    1.0,
  ],
  "line-opacity": DEPTH_LINE_OPACITY,
};

/**
 * Shallow→deep fill bands (source-layer depth_fill).
 * Soft wash: pale blue inshore → white offshore (low chroma).
 */
const DEPTH_SHALLOW_FILL = "#c5d9e6";
const DEPTH_BAND_PAINT: maplibregl.FillLayerSpecification["paint"] = {
  "fill-color": [
    "match",
    ["get", "band"],
    "0-2",
    DEPTH_SHALLOW_FILL,
    "2-4",
    "#d2e2ec",
    "4-5",
    "#dde9f0",
    "5-10",
    "#e8f0f5",
    "10-50",
    "#f3f7fa",
    "50+",
    "#ffffff",
    "#ffffff",
  ],
  "fill-opacity": DEPTH_FILL_OPACITY,
};

const DEPTH_LABEL_LAYOUT: maplibregl.SymbolLayerSpecification["layout"] = {
  "symbol-placement": "line",
  "symbol-spacing": 280,
  "text-field": ["concat", ["to-string", ["get", "depth"]], "m"],
  "text-size": ["interpolate", ["linear"], ["zoom"], 8, 9, 12, 10],
  "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
  "text-max-angle": 30,
  "text-optional": true,
  "text-allow-overlap": false,
  "text-padding": 2,
};

const DEPTH_LABEL_PAINT: maplibregl.SymbolLayerSpecification["paint"] = {
  "text-color": "#475569",
  "text-halo-width": 0,
  "text-opacity": DEPTH_TEXT_OPACITY,
};

export function buildChartStyle(_origin?: string): maplibregl.StyleSpecification {
  const coastUrl = chartPmtilesUrl("coastline.pmtiles");
  const depthUrl = chartPmtilesUrl("depth.pmtiles");

  return {
    version: 8,
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    transition: { duration: 0, delay: 0 },
    sources: {
      coastline: { type: "vector", url: coastUrl },
      depth: { type: "vector", url: depthUrl },
    },
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": "#ffffff" },
      },
      {
        id: "land-fill",
        type: "fill",
        source: "coastline",
        "source-layer": "land",
        paint: {
          "fill-color": LAND_FILL_COLOR,
          "fill-opacity": 1,
        },
      },
      // OSM split cells leave ~150 m gaps; paint those cuts in the land colour.
      {
        id: "land-seams",
        type: "line",
        source: "coastline",
        "source-layer": "land_seams",
        layout: {
          "line-join": "round",
          "line-cap": "round",
        },
        paint: {
          "line-color": LAND_FILL_COLOR,
          "line-opacity": 1,
          "line-width": [
            "interpolate",
            ["exponential", 2],
            ["zoom"],
            5,
            0.8,
            8,
            1.6,
            10,
            3.2,
            12,
            8,
            14,
            24,
          ],
        },
      },
      // Baked hillshade is inserted before water-fill at runtime. A
      // second land-cover above the depth bands keeps islands opaque once
      // the 0–5 m fills turn on at z9.
      {
        id: "depth-fill",
        type: "fill",
        source: "depth",
        "source-layer": "depth_fill",
        minzoom: 4,
        paint: DEPTH_BAND_PAINT,
      },
      {
        id: "depth-line",
        type: "line",
        source: "depth",
        "source-layer": "depth",
        minzoom: 4,
        paint: DEPTH_LINE_PAINT,
      },
      {
        id: "depth-label",
        type: "symbol",
        source: "depth",
        "source-layer": "depth",
        minzoom: 8,
        layout: DEPTH_LABEL_LAYOUT,
        paint: DEPTH_LABEL_PAINT,
      },
      // Estuaries / riverbanks / harbour basins — shallow blue when no bathymetry.
      {
        id: "water-fill",
        type: "fill",
        source: "coastline",
        "source-layer": "water",
        paint: {
          "fill-color": DEPTH_SHALLOW_FILL,
          "fill-opacity": 1,
        },
      },
      // Shore outline from the same tiles as land-fill (split-grid cuts live in land_seams).
      {
        id: "coastline",
        type: "line",
        source: "coastline",
        "source-layer": "coastline",
        layout: {
          "line-join": "round",
          "line-cap": "round",
        },
        paint: {
          "line-color": COASTLINE_COLOR,
          "line-opacity": 0.85,
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            4,
            0.3,
            7,
            0.5,
            10,
            0.8,
            14,
            1.4,
          ],
        },
      },
      // Windy-like subtle roads / rivers on land (under labels).
      {
        id: "roads",
        type: "line",
        source: "coastline",
        "source-layer": "roads",
        minzoom: 5,
        paint: {
          "line-color": "#94a3b8",
          "line-opacity": [
            "interpolate",
            ["linear"],
            ["zoom"],
            5,
            0.18,
            9,
            0.28,
            13,
            0.36,
          ],
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            5,
            ["match", ["get", "kind"], "motorway", 1.2, "trunk", 1.0, 0.5],
            10,
            [
              "match",
              ["get", "kind"],
              "motorway",
              2.2,
              "trunk",
              1.6,
              "primary",
              1.2,
              0.7,
            ],
            14,
            [
              "match",
              ["get", "kind"],
              "motorway",
              3.2,
              "trunk",
              2.4,
              "primary",
              1.8,
              1.1,
            ],
          ],
        },
      },
      {
        id: "rivers",
        type: "line",
        source: "coastline",
        "source-layer": "rivers",
        minzoom: 9,
        paint: {
          // Faint grey on white chart water.
          "line-color": "#cbd5e1",
          "line-opacity": 0.45,
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            9,
            0.4,
            14,
            0.9,
          ],
        },
      },
      {
        id: "river-labels",
        type: "symbol",
        source: "coastline",
        "source-layer": "rivers",
        minzoom: 9,
        layout: {
          "symbol-placement": "line",
          "symbol-spacing": 320,
          "text-field": ["get", "name"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 9, 9, 13, 11],
          "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
          "text-max-angle": 30,
          "text-optional": true,
          "text-padding": 2,
        },
        paint: {
          "text-color": "#64748b",
          "text-halo-width": 0,
          "text-opacity": 0.75,
        },
        filter: ["!=", ["get", "name"], ""],
      },
      {
        id: "place-labels",
        type: "symbol",
        source: "coastline",
        "source-layer": "places",
        minzoom: 4,
        layout: {
          "text-field": ["get", "name"],
          "text-size": [
            "interpolate",
            ["linear"],
            ["zoom"],
            4,
            ["match", ["get", "kind"], "city", 12, "island", 11, "town", 10, "harbour", 10, 9],
            9,
            [
              "match",
              ["get", "kind"],
              "city",
              14,
              "island",
              13,
              "town",
              12,
              "harbour",
              11,
              10,
            ],
            13,
            [
              "match",
              ["get", "kind"],
              "city",
              16,
              "island",
              14,
              "town",
              13,
              "harbour",
              12,
              11,
            ],
          ],
          "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
          "text-anchor": "center",
          "text-optional": true,
          "text-padding": 2,
          "text-max-width": 10,
          "text-allow-overlap": false,
        },
        paint: {
          "text-color": "#334155",
          "text-halo-color": "rgba(15, 23, 42, 0.2)",
          "text-halo-width": 0.8,
          "text-halo-blur": 0.6,
          "text-opacity": 0.92,
        },
      },
    ],
  };
}

function routesToFeatureCollection(routes: ModelRoute[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes.map((r) => ({
      type: "Feature",
      properties: { model_id: r.model_id, color: colorForModel(r.model_id) },
      geometry: {
        type: "LineString",
        coordinates: r.legs.map((leg) => [leg.position[1], leg.position[0]]),
      },
    })),
  };
}

function maneuverPoints(routes: ModelRoute[]): GeoJSON.FeatureCollection {
  const feats: GeoJSON.Feature[] = [];
  for (const r of routes) {
    const { legs } = r;
    for (let i = 0; i < legs.length; i++) {
      const leg = legs[i];
      if (leg.maneuver === "none") continue;
      // Tack/gybe is tagged on the post-turn child; the geometric turn is at the parent.
      // Marks stay on the mark node itself.
      const at =
        leg.maneuver !== "mark" && i > 0 ? legs[i - 1].position : leg.position;
      feats.push({
        type: "Feature",
        properties: { maneuver: leg.maneuver, color: colorForModel(r.model_id) },
        geometry: { type: "Point", coordinates: [at[1], at[0]] },
      });
    }
  }
  return { type: "FeatureCollection", features: feats };
}

function boatFeature(
  position: [number, number] | null,
  headingDeg: number | null,
): GeoJSON.FeatureCollection {
  if (!position) return { type: "FeatureCollection", features: [] };
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { heading_deg: headingDeg ?? 0 },
        geometry: { type: "Point", coordinates: [position[1], position[0]] },
      },
    ],
  };
}

function debounce<T extends (...args: never[]) => void>(fn: T, ms: number): T {
  let t: ReturnType<typeof setTimeout> | null = null;
  return ((...args: never[]) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  }) as T;
}

function twdFromUv(u: number, v: number): number {
  return ((Math.atan2(-u, -v) * 180) / Math.PI + 360) % 360;
}

function formatCoursePointTooltip(
  kind: string,
  label: string,
  lat: number,
  lon: number,
): string {
  const name =
    kind === "start" ? "Start" : kind === "finish" ? "Finish" : `Mark ${label}`;
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `<div class="course-point-tooltip">
    <strong>${name}</strong>
    <div class="course-point-tooltip-coords">${Math.abs(lat).toFixed(4)}°${ns} ${Math.abs(lon).toFixed(4)}°${ew}</div>
  </div>`;
}

export default function MapView({
  routes = [],
  course,
  showWind = false,
  windTimeUtc,
  windSeries = null,
  playbackPosition = null,
  playbackHeading = null,
  livePosition = null,
  liveHeading = null,
  planPosition = null,
  planHeading = null,
  fitRouteKey,
  initialBounds = null,
  editMode = "none",
  showDepth = true,
  onMapClick,
  onRouteSelect,
  onRouteSeek,
  onCoursePointMove,
  onOverlayError,
  onViewportChange,
  onBootReady,
}: {
  routes?: ModelRoute[];
  course?: Course | null;
  showWind?: boolean;
  windTimeUtc?: string;
  /** Multi-hour field — browse + playback both slice locally (no tile fetches). */
  windSeries?: WindSeries | null;
  playbackPosition?: [number, number] | null;
  playbackHeading?: number | null;
  /** GPS fix. Drawn as the "you" marker when live mode is on. */
  livePosition?: [number, number] | null;
  liveHeading?: number | null;
  /** Planned boat at the current clock, when it is not already the playback boat. */
  planPosition?: [number, number] | null;
  planHeading?: number | null;
  fitRouteKey?: string;
  /** Optional first-paint camera (restored course/route) before style load. */
  initialBounds?: MapBounds | null;
  editMode?: EditMode;
  showDepth?: boolean;
  onMapClick?: (lat: number, lon: number) => void;
  onRouteSelect?: (modelId: string) => void;
  onRouteSeek?: (modelId: string, tUtc: string) => void;
  onCoursePointMove?: (
    kind: "start" | "finish" | "mark",
    index: number | null,
    lat: number,
    lon: number,
  ) => void;
  onOverlayError?: (message: string | null) => void;
  onViewportChange?: (bounds: MapBounds, maxSpanMiles: number, zoom: number) => void;
  /** Fires once when the map is framed and ready to reveal (boot cover). */
  onBootReady?: () => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const loadedRef = useRef(false);
  const syncLocalRef = useRef<(() => void) | null>(null);
  const onRouteSelectRef = useRef(onRouteSelect);
  const onRouteSeekRef = useRef(onRouteSeek);
  const onMapClickRef = useRef(onMapClick);
  const onCoursePointMoveRef = useRef(onCoursePointMove);
  const onOverlayErrorRef = useRef(onOverlayError);
  const onViewportChangeRef = useRef(onViewportChange);
  const editModeRef = useRef(editMode);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const hoverPopupRef = useRef<maplibregl.Popup | null>(null);
  const dragPointRef = useRef<{
    kind: "start" | "finish" | "mark";
    index: number | null;
  } | null>(null);

  const windHeatRef = useRef<WindHeatmapLayer | null>(null);
  const windArrowsRef = useRef<WindArrowLayer | null>(null);
  const windFlowRef = useRef<WindFlowLayer | null>(null);
  const applyWindVizRef = useRef<(() => void) | null>(null);
  const windGridRef = useRef<MeteoGrid | null>(null);
  const windDataRef = useRef(false);
  const terrainTilesReadyRef = useRef(false);
  const terrainTileBoundsRef = useRef<DemBounds | null>(null);

  const flewToLiveRef = useRef(false);
  const fitRouteKeyRef = useRef(fitRouteKey ?? "");
  fitRouteKeyRef.current = fitRouteKey ?? "";
  const applyFitRef = useRef<(() => void) | null>(null);
  const onBootReadyRef = useRef(onBootReady);
  onBootReadyRef.current = onBootReady;
  const bootDoneRef = useRef(false);
  const signalBootReadyRef = useRef(() => {
    if (bootDoneRef.current) return;
    bootDoneRef.current = true;
    onBootReadyRef.current?.();
  });
  const initialBoundsRef = useRef(initialBounds);
  // Capture first-mount bounds only — later prop updates must not recreate the map.
  if (initialBounds && !initialBoundsRef.current) {
    initialBoundsRef.current = initialBounds;
  }

  const propsRef = useRef({
    routes,
    course,
    showWind,
    showDepth,
    windTimeUtc,
    windSeries,
    playbackPosition,
    playbackHeading,
    livePosition,
    liveHeading,
    planPosition,
    planHeading,
  });

  propsRef.current = {
    routes,
    course,
    showWind,
    showDepth,
    windTimeUtc,
    windSeries,
    playbackPosition,
    playbackHeading,
    livePosition,
    liveHeading,
    planPosition,
    planHeading,
  };
  onRouteSelectRef.current = onRouteSelect;
  onRouteSeekRef.current = onRouteSeek;
  onMapClickRef.current = onMapClick;
  onCoursePointMoveRef.current = onCoursePointMove;
  onOverlayErrorRef.current = onOverlayError;
  onViewportChangeRef.current = onViewportChange;
  editModeRef.current = editMode;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    ensurePmtilesProtocol();

    const bootCam = initialBoundsRef.current
      ? cameraForBounds(initialBoundsRef.current)
      : { center: REGION_CENTER, zoom: DEFAULT_ZOOM };

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: buildChartStyle(window.location.origin),
      center: bootCam.center,
      zoom: bootCam.zoom,
      minZoom: MIN_ZOOM,
      maxZoom: 18,
      maxPitch: 60,
      maxBounds: REGION_MAX_BOUNDS,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    // Arrow keys are used by TimeScrubber for route stepping — don't pan/zoom.
    map.keyboard.disable();
    // Pitch for DEM / cliffs. Rotation stays off until the camera is pitched.
    map.touchZoomRotate.disableRotation();
    map.touchPitch.enable();
    let bearingResetting = false;
    const rotationAllowed = () => map.getPitch() > ROTATE_ENABLE_PITCH_DEG;
    const applyRotationGate = () => {
      if (rotationAllowed()) {
        map.touchZoomRotate.enableRotation();
        return;
      }
      map.touchZoomRotate.disableRotation();
      if (bearingResetting) return;
      if (Math.abs(map.getBearing()) < 0.05) {
        if (map.getBearing() !== 0) map.setBearing(0);
        return;
      }
      bearingResetting = true;
      map.easeTo({ bearing: 0, duration: 300 });
      map.once("moveend", () => {
        bearingResetting = false;
        if (!rotationAllowed() && Math.abs(map.getBearing()) > 0.05) {
          map.setBearing(0);
        }
      });
    };
    map.on("rotate", () => {
      if (bearingResetting) return;
      if (!rotationAllowed() && map.getBearing() !== 0) map.setBearing(0);
    });
    map.on("pitch", () => {
      if (rotationAllowed()) map.touchZoomRotate.enableRotation();
      syncTerrainAndWind();
    });

    map.on("error", (e) => {
      const msg = e.error?.message ?? String(e.error ?? "Map error");
      console.warn("[map]", msg, e);
      onOverlayErrorRef.current?.(msg);
    });

    const windHeat = new WindHeatmapLayer();
    const windFlow = new WindFlowLayer();
    const windArrows = new WindArrowLayer();
    windHeatRef.current = windHeat;
    windFlowRef.current = windFlow;
    windArrowsRef.current = windArrows;

    const applyFit = () => {
      const key = fitRouteKeyRef.current;
      if (!loadedRef.current) return;

      const routeBox = boundsFromCourseAndRoutes(
        propsRef.current.course,
        propsRef.current.routes,
      );
      if (!key || !routeBox) {
        signalBootReadyRef.current();
        return;
      }

      const view = boundsFromMap(() => map.getBounds());
      const forceFrame = key.startsWith("load-") || key.startsWith("frame-");
      if (!forceFrame && bboxMeaningfullyFramed(view, routeBox)) {
        signalBootReadyRef.current();
        return;
      }

      map.fitBounds(
        [
          [routeBox.lon_min, routeBox.lat_min],
          [routeBox.lon_max, routeBox.lat_max],
        ],
        {
          padding: 56,
          duration: forceFrame ? 0 : 600,
          maxZoom: Math.max(map.getZoom(), FIT_MAX_ZOOM),
        },
      );
      if (forceFrame) {
        map.once("idle", () => signalBootReadyRef.current());
      } else {
        signalBootReadyRef.current();
      }
    };
    applyFitRef.current = applyFit;

    map.on("load", async () => {
      loadedRef.current = true;
      // Prefer framing a restored course/route over flying to a lone GPS fix.
      applyFit();
      const firstLive = propsRef.current.livePosition;
      if (
        firstLive &&
        !flewToLiveRef.current &&
        !fitRouteKeyRef.current
      ) {
        flewToLiveRef.current = true;
        map.flyTo({
          center: [firstLive[1], firstLive[0]],
          zoom: Math.max(map.getZoom(), 11),
        });
      }
      if (!fitRouteKeyRef.current) {
        map.once("idle", () => signalBootReadyRef.current());
      }
      const empty = { type: "FeatureCollection" as const, features: [] };

      // Depth → land → wind → routes (forecast above chart, under route overlays)
      windHeat.attach(map);

      try {
        const arrowImg = await svgToImage(WIND_ARROW_SVG, 48);
        if (map.hasImage(WIND_ARROW_ICON_ID)) map.removeImage(WIND_ARROW_ICON_ID);
        map.addImage(WIND_ARROW_ICON_ID, arrowImg, { pixelRatio: 2 });
      } catch (e) {
        console.warn("[map] wind arrow icon failed to load", e);
      }
      windFlow.attach(map);
      windArrows.attach(map);

      const terrainMeta = await fetchTerrainTileMeta(window.location.origin);
      if (terrainMeta) {
        terrainTilesReadyRef.current = true;
        terrainTileBoundsRef.current = {
          lon_min: terrainMeta.bounds[0],
          lat_min: terrainMeta.bounds[1],
          lon_max: terrainMeta.bounds[2],
          lat_max: terrainMeta.bounds[3],
        };
        attachTerrainLayers(map, window.location.origin, terrainMeta);
      }
      // Cover OSM land again so depth bands cannot show through islands, then
      // put baked hillshade on top of that fill (under water / rivers).
      const beforeWater = map.getLayer("water-fill")
        ? "water-fill"
        : map.getLayer("coastline")
          ? "coastline"
          : undefined;
      if (!map.getLayer("land-cover")) {
        map.addLayer(
          {
            id: "land-cover",
            type: "fill",
            source: "coastline",
            "source-layer": "land",
            paint: {
              "fill-color": LAND_FILL_COLOR,
              "fill-opacity": 1,
            },
          },
          beforeWater,
        );
      }
      if (map.getLayer("land-seams") && beforeWater) {
        map.moveLayer("land-seams", beforeWater);
      }
      if (map.getLayer(TERRAIN_SHADE_LAYER_ID) && beforeWater) {
        map.moveLayer(TERRAIN_SHADE_LAYER_ID, beforeWater);
      }

      map.addSource("routes", { type: "geojson", data: routesToFeatureCollection([]) });
      map.addLayer({
        id: "routes",
        source: "routes",
        type: "line",
        paint: {
          "line-color": ["get", "color"],
          "line-width": ["interpolate", ["linear"], ["zoom"], 6, 2, 12, 5],
          "line-opacity": 0.92,
        },
      });

      map.addSource("maneuvers", { type: "geojson", data: maneuverPoints([]) });
      map.addLayer({
        id: "maneuvers",
        source: "maneuvers",
        type: "circle",
        paint: {
          "circle-radius": 4,
          "circle-color": ["get", "color"],
          "circle-stroke-color": "#0b1220",
          "circle-stroke-width": 1,
        },
      });

      map.addSource("course", { type: "geojson", data: empty });
      map.addLayer({
        id: "course-marks",
        source: "course",
        type: "circle",
        paint: {
          "circle-radius": 10,
          "circle-color": [
            "match",
            ["get", "kind"],
            "start",
            "#4ade80",
            "finish",
            "#fb7185",
            "#1e3a5f",
          ],
          "circle-stroke-color": "#0b1220",
          "circle-stroke-width": 2,
        },
      });
      map.addLayer({
        id: "course-labels",
        source: "course",
        type: "symbol",
        layout: {
          "text-field": ["get", "label"],
          "text-size": 11,
          "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
          "text-allow-overlap": true,
          "text-ignore-placement": true,
          "text-anchor": "center",
        },
        paint: {
          "text-color": [
            "match",
            ["get", "kind"],
            "mark",
            "#ffffff",
            "#0b1220",
          ],
        },
      });

      try {
        const boatImg = await svgToImage(BOAT_SVG, 48);
        if (!map.hasImage("boat")) {
          map.addImage("boat", boatImg, { pixelRatio: 2 });
        }
      } catch (e) {
        console.warn("[map] boat icon failed to load", e);
      }

      map.addSource("playback-boat", { type: "geojson", data: empty });
      map.addLayer({
        id: "playback-boat",
        source: "playback-boat",
        type: "symbol",
        layout: {
          "icon-image": "boat",
          "icon-size": ["interpolate", ["linear"], ["zoom"], 5, 0.4, 10, 0.85, 14, 1.1],
          "icon-rotate": ["get", "heading_deg"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });

      map.addSource("live-link", { type: "geojson", data: empty });
      map.addLayer({
        id: "live-link",
        source: "live-link",
        type: "line",
        paint: {
          "line-color": "#22d3ee",
          "line-width": 1.5,
          "line-dasharray": [2, 2],
          "line-opacity": 0.9,
        },
      });
      map.addSource("live-plan", { type: "geojson", data: empty });
      map.addLayer({
        id: "live-plan",
        source: "live-plan",
        type: "symbol",
        layout: {
          "icon-image": "boat",
          "icon-size": ["interpolate", ["linear"], ["zoom"], 5, 0.35, 10, 0.75, 14, 1],
          "icon-rotate": ["get", "heading_deg"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
        paint: { "icon-opacity": 0.45 },
      });
      map.addSource("live-you", { type: "geojson", data: empty });
      map.addLayer({
        id: "live-you-halo",
        source: "live-you",
        type: "circle",
        paint: {
          "circle-radius": 11,
          "circle-color": "#22d3ee",
          "circle-opacity": 0.35,
        },
      });
      map.addLayer({
        id: "live-you",
        source: "live-you",
        type: "circle",
        paint: {
          "circle-radius": 6,
          "circle-color": "#22d3ee",
          "circle-stroke-color": "#f8fafc",
          "circle-stroke-width": 2,
        },
      });

      map.on("click", "routes", (e) => {
        const f = e.features?.[0];
        const mid = f?.properties?.model_id;
        if (!mid) return;
        const modelId = String(mid);
        onRouteSelectRef.current?.(modelId);
        const seek = onRouteSeekRef.current;
        if (seek && e.lngLat) {
          const route = propsRef.current.routes.find((r) => r.model_id === modelId);
          if (route?.legs?.length) {
            const tUtc = seekTimeAlongRoute(route.legs, e.lngLat.lat, e.lngLat.lng);
            if (tUtc) seek(modelId, tUtc);
          }
        }
      });
      map.on("mouseenter", "routes", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "routes", () => {
        map.getCanvas().style.cursor = courseCursorCss(editModeRef.current);
      });

      const showCoursePointTooltip = (
        e: maplibregl.MapLayerMouseEvent,
      ) => {
        if (dragPointRef.current != null) return;
        if (editModeRef.current !== "none") {
          map.getCanvas().style.cursor = courseCursorCss(editModeRef.current);
          return;
        }
        map.getCanvas().style.cursor = "move";
        const f = e.features?.[0];
        if (!f || f.geometry.type !== "Point") return;
        const kind = String(f.properties?.kind ?? "");
        const label = String(f.properties?.label ?? "");
        const [lon, lat] = f.geometry.coordinates as [number, number];
        hoverPopupRef.current?.remove();
        hoverPopupRef.current = new maplibregl.Popup({
          closeButton: false,
          closeOnClick: false,
          offset: 14,
          className: "course-point-tooltip-wrap",
          maxWidth: "220px",
        })
          .setLngLat([lon, lat])
          .setHTML(formatCoursePointTooltip(kind, label, lat, lon))
          .addTo(map);
      };

      const hideCoursePointTooltip = () => {
        if (dragPointRef.current != null) return;
        map.getCanvas().style.cursor = courseCursorCss(editModeRef.current);
        hoverPopupRef.current?.remove();
        hoverPopupRef.current = null;
      };

      map.on("mouseenter", "course-marks", showCoursePointTooltip);
      map.on("mouseleave", "course-marks", hideCoursePointTooltip);
      map.on("mouseenter", "course-labels", showCoursePointTooltip);
      map.on("mouseleave", "course-labels", hideCoursePointTooltip);

      map.on("mousedown", "course-marks", (e) => {
        if (editModeRef.current !== "none") return;
        const f = e.features?.[0];
        const kind = f?.properties?.kind;
        if (kind !== "start" && kind !== "finish" && kind !== "mark") return;
        let index: number | null = null;
        if (kind === "mark") {
          index = Number(f?.properties?.index);
          if (!Number.isFinite(index)) return;
        }
        e.preventDefault();
        hoverPopupRef.current?.remove();
        hoverPopupRef.current = null;
        dragPointRef.current = { kind, index };
        map.dragPan.disable();
        map.scrollZoom.disable();
        map.getCanvas().style.cursor = "grabbing";
      });
      map.on("mousedown", "course-labels", (e) => {
        if (editModeRef.current !== "none") return;
        const f = e.features?.[0];
        const kind = f?.properties?.kind;
        if (kind !== "start" && kind !== "finish" && kind !== "mark") return;
        let index: number | null = null;
        if (kind === "mark") {
          index = Number(f?.properties?.index);
          if (!Number.isFinite(index)) return;
        }
        e.preventDefault();
        hoverPopupRef.current?.remove();
        hoverPopupRef.current = null;
        dragPointRef.current = { kind, index };
        map.dragPan.disable();
        map.scrollZoom.disable();
        map.getCanvas().style.cursor = "grabbing";
      });

      map.on("mousemove", (e) => {
        const drag = dragPointRef.current;
        if (!drag) return;
        onCoursePointMoveRef.current?.(drag.kind, drag.index, e.lngLat.lat, e.lngLat.lng);
      });

      map.on("mouseup", () => {
        if (dragPointRef.current == null) return;
        dragPointRef.current = null;
        map.dragPan.enable();
        map.scrollZoom.enable();
        map.getCanvas().style.cursor = courseCursorCss(editModeRef.current);
      });

      // Heatmap with the land stack so 3D terrain drape rides the mesh; flow/arrows under routes.
      try {
        const heatBefore = map.getLayer("coastline")
          ? "coastline"
          : map.getLayer("roads")
            ? "roads"
            : map.getLayer("routes")
              ? "routes"
              : undefined;
        if (map.getLayer("wind-heatmap-canvas") && heatBefore) {
          map.moveLayer("wind-heatmap-canvas", heatBefore);
        }
        for (const id of [
          WIND_FLOW_TAIL_HALO_LAYER_ID,
          WIND_FLOW_TAIL_LAYER_ID,
          WIND_FLOW_ARROW_LAYER_ID,
        ]) {
          if (map.getLayer(id) && map.getLayer("routes")) map.moveLayer(id, "routes");
        }
        if (map.getLayer("wind-arrows") && map.getLayer("routes")) {
          map.moveLayer("wind-arrows", "routes");
        }
        if (map.getLayer("place-labels") && map.getLayer("routes")) {
          map.moveLayer("place-labels", "routes");
        }
      } catch {
        /* layer order optional */
      }

      emitViewport();
      syncLocalData();
      applyWindVizRef.current?.();

      setDepthOpacity(map, propsRef.current.showDepth);
    });

    const debouncedEmitViewport = debounce(() => {
      emitViewport();
    }, 150);

    const applyWindViz = () => {
      const m = mapRef.current;
      const grid = windGridRef.current;
      const windOn = !!grid && propsRef.current.showWind && windDataRef.current;

      if (m) windArrowsRef.current?.setVisible(m, false);

      if (windOn && grid) {
        if (m) windFlowRef.current?.setVisible(m, true);
        windFlowRef.current?.setGrid(grid);
      } else {
        if (m) windFlowRef.current?.setVisible(m, false);
        windFlowRef.current?.setGrid(null);
      }
    };
    applyWindVizRef.current = applyWindViz;
    if (map.loaded()) applyWindViz();

    map.on("moveend", () => {
      debouncedEmitViewport();
      syncTerrainAndWind();
    });
    map.on("sourcedata", (e) => {
      if (e.sourceId === TERRAIN_SOURCE_ID && e.sourceDataType === "idle") {
        syncTerrainAndWind(true);
      }
    });
    map.on("pitchend", () => {
      applyRotationGate();
      syncTerrainAndWind();
      syncLocalData();
    });

    map.on("click", async (e) => {
      if (dragPointRef.current != null) return;
      // Placing start/finish/marks always wins over route or mark hit-tests.
      if (editModeRef.current !== "none" && onMapClickRef.current) {
        onMapClickRef.current(e.lngLat.lat, e.lngLat.lng);
        return;
      }
      const markHit = map.queryRenderedFeatures(e.point, { layers: ["course-marks"] });
      if (
        markHit.some(
          (f) =>
            f.properties?.kind === "mark" ||
            f.properties?.kind === "start" ||
            f.properties?.kind === "finish",
        )
      ) {
        return;
      }
      const feats = map.queryRenderedFeatures(e.point, { layers: ["routes"] });
      if (feats.length > 0) return;
      if (onMapClickRef.current) {
        onMapClickRef.current(e.lngLat.lat, e.lngLat.lng);
        return;
      }

      const lat = e.lngLat.lat;
      const lon = e.lngLat.lng;

      let tws: number | null = null;
      let twd: number | null = null;
      const grid = windGridRef.current;
      if (grid) {
        const sample = sampleGrid(grid, lat, lon);
        if (sample.speed >= 0.5) {
          tws = Math.round(sample.speed * 10) / 10;
          twd = Math.round(twdFromUv(sample.u, sample.v) * 10) / 10;
        }
      }

      let data: PointInspect = { lat, lon, tws, twd };
      try {
        const elev = await api.elevation(lat, lon);
        data = { ...data, ...elev };
      } catch {
        /* offline */
      }

      popupRef.current?.remove();
      popupRef.current = new maplibregl.Popup({
        maxWidth: "280px",
        className: "map-click-popup-wrap",
        closeButton: true,
      })
        .setLngLat(e.lngLat)
        .setHTML(formatPointPopup(data))
        .addTo(map);
    });

    function emitViewport() {
      const m = mapRef.current;
      if (!m) return;
      const bbox = boundsFromMap(() => m.getBounds());
      const span = viewportMaxSpanMiles(bbox);
      onViewportChangeRef.current?.(bbox, span, m.getZoom());
    }

    function syncTerrainAndWind(forceMesh = false) {
      const m = mapRef.current;
      if (!m || !loadedRef.current) return;

      const zoom = m.getZoom();
      const b = m.getBounds();
      const tileBounds = terrainTileBoundsRef.current;
      const overTerrainTiles =
        tileBounds != null &&
        viewportIntersectsAnyDem(
          b.getWest(),
          b.getSouth(),
          b.getEast(),
          b.getNorth(),
          [tileBounds],
        );

      const windOn = propsRef.current.showWind && windDataRef.current;

      let draped = false;
      if (terrainTilesReadyRef.current && m.getSource(TERRAIN_SOURCE_ID)) {
        const wantMesh =
          zoom >= TERRAIN_MESH_MIN_ZOOM &&
          overTerrainTiles &&
          m.getPitch() > MESH_PITCH_MIN_DEG;
        const hasTerrain = m.getTerrain() != null;
        if (wantMesh && (!hasTerrain || forceMesh)) {
          m.setTerrain({
            source: TERRAIN_SOURCE_ID,
            exaggeration: TERRAIN_EXAGGERATION,
          });
        } else if (!wantMesh && hasTerrain) {
          m.setTerrain(null);
        }
        draped = wantMesh;
        // Relief raster stays on in 2D and draped on the mesh in 3D.
        if (m.getLayer(TERRAIN_SHADE_LAYER_ID)) {
          m.setLayoutProperty(
            TERRAIN_SHADE_LAYER_ID,
            "visibility",
            overTerrainTiles ? "visible" : "none",
          );
        }
      }

      windHeatRef.current?.setVisible(m, windOn);
      windHeatRef.current?.setDraped(m, draped);
      windArrowsRef.current?.setVisible(m, false);
      windFlowRef.current?.setTerrainActive(draped);
      windFlowRef.current?.setVisible(m, windOn);
    }

    function syncLocalData() {
      const m = mapRef.current;
      if (!m || !loadedRef.current) return;
      const p = propsRef.current;

      (m.getSource("routes") as maplibregl.GeoJSONSource | undefined)?.setData(
        routesToFeatureCollection(p.routes),
      );
      (m.getSource("maneuvers") as maplibregl.GeoJSONSource | undefined)?.setData(
        maneuverPoints(p.routes),
      );

      const courseFeatures: GeoJSON.Feature[] = [];
      if (p.course) {
        if (isPointSet(p.course.start)) {
          courseFeatures.push({
            type: "Feature",
            properties: { kind: "start", label: "S" },
            geometry: { type: "Point", coordinates: [p.course.start[1], p.course.start[0]] },
          });
        }
        p.course.marks.forEach((mk, i) => {
          if (!isPointSet(mk.position)) return;
          courseFeatures.push({
            type: "Feature",
            properties: { kind: "mark", label: `M${i + 1}`, index: i },
            geometry: { type: "Point", coordinates: [mk.position[1], mk.position[0]] },
          });
        });
        if (isPointSet(p.course.finish)) {
          courseFeatures.push({
            type: "Feature",
            properties: { kind: "finish", label: "F" },
            geometry: { type: "Point", coordinates: [p.course.finish[1], p.course.finish[0]] },
          });
        }
      }
      (m.getSource("course") as maplibregl.GeoJSONSource | undefined)?.setData({
        type: "FeatureCollection",
        features: courseFeatures,
      });

      (m.getSource("playback-boat") as maplibregl.GeoJSONSource | undefined)?.setData(
        boatFeature(p.playbackPosition, p.playbackHeading),
      );
      (m.getSource("live-you") as maplibregl.GeoJSONSource | undefined)?.setData(
        boatFeature(p.livePosition, p.liveHeading),
      );
      const planOnPlayback =
        p.planPosition != null &&
        p.playbackPosition != null &&
        Math.abs(p.planPosition[0] - p.playbackPosition[0]) < 1e-5 &&
        Math.abs(p.planPosition[1] - p.playbackPosition[1]) < 1e-5;
      const noFeatures = { type: "FeatureCollection" as const, features: [] };
      (m.getSource("live-plan") as maplibregl.GeoJSONSource | undefined)?.setData(
        planOnPlayback ? noFeatures : boatFeature(p.planPosition, p.planHeading),
      );
      const link =
        p.livePosition && p.planPosition
          ? {
              type: "FeatureCollection" as const,
              features: [
                {
                  type: "Feature" as const,
                  properties: {},
                  geometry: {
                    type: "LineString" as const,
                    coordinates: [
                      [p.livePosition[1], p.livePosition[0]],
                      [p.planPosition[1], p.planPosition[0]],
                    ],
                  },
                },
              ],
            }
          : noFeatures;
      (m.getSource("live-link") as maplibregl.GeoJSONSource | undefined)?.setData(link);

      syncTerrainAndWind();
    }

    syncLocalRef.current = () => syncLocalData();

    const endPointDragWindow = () => {
      if (dragPointRef.current == null) return;
      dragPointRef.current = null;
      const m = mapRef.current;
      if (!m) return;
      m.dragPan.enable();
      m.scrollZoom.enable();
      m.getCanvas().style.cursor = courseCursorCss(editModeRef.current);
    };
    window.addEventListener("mouseup", endPointDragWindow);

    return () => {
      window.removeEventListener("mouseup", endPointDragWindow);
      hoverPopupRef.current?.remove();
      popupRef.current?.remove();
      windHeatRef.current?.destroy(map);
      windFlowRef.current?.destroy(map);
      windArrowsRef.current?.destroy(map);
      map.remove();
      mapRef.current = null;
      loadedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!livePosition) {
      flewToLiveRef.current = false;
      return;
    }
    const m = mapRef.current;
    if (!m || !loadedRef.current || flewToLiveRef.current) return;
    flewToLiveRef.current = true;
    m.flyTo({
      center: [livePosition[1], livePosition[0]],
      zoom: Math.max(m.getZoom(), 11),
    });
  }, [livePosition]);

  useEffect(() => {
    syncLocalRef.current?.();
  }, [
    routes,
    course,
    playbackPosition,
    playbackHeading,
    livePosition,
    liveHeading,
    planPosition,
    planHeading,
    showWind,
  ]);

  useEffect(() => {
    const m = mapRef.current;
    if (!m || !loadedRef.current) return;
    const cursor = courseCursorCss(editMode);
    m.getCanvas().style.cursor = cursor;
    return () => {
      if (mapRef.current) mapRef.current.getCanvas().style.cursor = "";
    };
  }, [editMode]);

  // Browse + playback: slice the series locally — no network.
  useEffect(() => {
    const m = mapRef.current;
    if (!m || !loadedRef.current) return;

    if (!showWind) {
      windDataRef.current = false;
      windGridRef.current = null;
      windHeatRef.current?.setGrid(null);
      applyWindVizRef.current?.();
      syncLocalRef.current?.();
      return;
    }

    if (windSeries && windTimeUtc) {
      const grid = sliceSeries(windSeries, windTimeUtc);
      windDataRef.current = true;
      windGridRef.current = grid;
      windHeatRef.current?.setGrid(grid);
      applyWindVizRef.current?.();
      syncLocalRef.current?.();
      return;
    }

    windDataRef.current = false;
    windGridRef.current = null;
    windHeatRef.current?.setGrid(null);
    applyWindVizRef.current?.();
    syncLocalRef.current?.();
  }, [windSeries, windTimeUtc, showWind]);

  useEffect(() => {
    applyFitRef.current?.();
  }, [fitRouteKey]);

  useEffect(() => {
    const m = mapRef.current;
    if (!m || !loadedRef.current) return;
    setDepthOpacity(m, showDepth);
  }, [showDepth]);

  return <div id="map" ref={containerRef} />;
}
