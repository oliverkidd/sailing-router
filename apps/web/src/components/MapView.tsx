"use client";

import maplibregl from "maplibre-gl";
import { Protocol } from "pmtiles";
import { useEffect, useRef } from "react";
import { api } from "@/lib/api";
import {
  boundsFromMap,
  courseIsReady,
  type MapBounds,
} from "@/lib/mapOverlays";
import { sampleGrid, sliceSeries, type MeteoGrid, type WindSeries } from "@/lib/meteoGrid";
import { WindHeatmapLayer } from "@/lib/windHeatmap";
import { WindArrowLayer, WIND_ARROW_ICON_ID } from "@/lib/windArrows";
import { colorForModel } from "@/lib/colors";
import { BOAT_SVG, WIND_ARROW_SVG, svgToImage } from "@/lib/mapIcons";
import { formatPointPopup, type PointInspect } from "@/lib/mapPopup";
import { REGION_CENTER, REGION_MAX_BOUNDS } from "@/lib/region";
import { viewportMaxSpanMiles } from "@/lib/windTiles";
import type { Course, ModelRoute } from "@/lib/types";

const DEFAULT_ZOOM = 5;
const MIN_ZOOM = 4;

let pmtilesRegistered = false;
function ensurePmtilesProtocol() {
  if (pmtilesRegistered) return;
  const protocol = new Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  pmtilesRegistered = true;
}

/** Dark nautical chart — land + depth isobaths from local PMTiles (no runtime fetch). */
/** Bump when coastline.pmtiles / depth.pmtiles are rebuilt so browsers skip stale Range caches. */
const CHART_TILE_VERSION = "20260712full";

/** Uniform isobath stroke — slate, low opacity. */
const DEPTH_LINE_COLOR = "#475569";
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
  "line-opacity": 0.32,
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
  "fill-opacity": 1,
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
  "text-opacity": 0.9,
};

function buildChartStyle(origin: string): maplibregl.StyleSpecification {
  const coastUrl = `pmtiles://${origin}/geo/coastline.pmtiles?v=${CHART_TILE_VERSION}`;
  const depthUrl = `pmtiles://${origin}/geo/depth.pmtiles?v=${CHART_TILE_VERSION}`;

  return {
    version: 8,
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
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
      {
        id: "land-fill",
        type: "fill",
        source: "coastline",
        "source-layer": "land",
        paint: {
          "fill-color": "#dddddd",
          "fill-opacity": 1,
        },
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
    for (const leg of r.legs) {
      if (leg.maneuver === "none") continue;
      feats.push({
        type: "Feature",
        properties: { maneuver: leg.maneuver, color: colorForModel(r.model_id) },
        geometry: { type: "Point", coordinates: [leg.position[1], leg.position[0]] },
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

export default function MapView({
  routes = [],
  course,
  showWind = false,
  windTimeUtc,
  windSeries = null,
  playbackPosition = null,
  playbackHeading = null,
  fitRouteKey,
  onMapClick,
  onRouteSelect,
  onOverlayError,
  onViewportChange,
}: {
  routes?: ModelRoute[];
  course?: Course | null;
  showWind?: boolean;
  windTimeUtc?: string;
  /** Multi-hour field — browse + playback both slice locally (no tile fetches). */
  windSeries?: WindSeries | null;
  playbackPosition?: [number, number] | null;
  playbackHeading?: number | null;
  fitRouteKey?: string;
  onMapClick?: (lat: number, lon: number) => void;
  onRouteSelect?: (modelId: string) => void;
  onOverlayError?: (message: string | null) => void;
  onViewportChange?: (bounds: MapBounds, maxSpanMiles: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const loadedRef = useRef(false);
  const syncLocalRef = useRef<(() => void) | null>(null);
  const onRouteSelectRef = useRef(onRouteSelect);
  const onMapClickRef = useRef(onMapClick);
  const onOverlayErrorRef = useRef(onOverlayError);
  const onViewportChangeRef = useRef(onViewportChange);
  const popupRef = useRef<maplibregl.Popup | null>(null);

  const windHeatRef = useRef<WindHeatmapLayer | null>(null);
  const windArrowsRef = useRef<WindArrowLayer | null>(null);
  const windGridRef = useRef<MeteoGrid | null>(null);
  const windDataRef = useRef(false);

  const propsRef = useRef({
    routes,
    course,
    showWind,
    windTimeUtc,
    windSeries,
    playbackPosition,
    playbackHeading,
  });

  propsRef.current = {
    routes,
    course,
    showWind,
    windTimeUtc,
    windSeries,
    playbackPosition,
    playbackHeading,
  };
  onRouteSelectRef.current = onRouteSelect;
  onMapClickRef.current = onMapClick;
  onOverlayErrorRef.current = onOverlayError;
  onViewportChangeRef.current = onViewportChange;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    ensurePmtilesProtocol();

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: buildChartStyle(window.location.origin),
      center: REGION_CENTER,
      zoom: DEFAULT_ZOOM,
      minZoom: MIN_ZOOM,
      maxZoom: 18,
      maxBounds: REGION_MAX_BOUNDS,
      attributionControl: { compact: true },
    });
    mapRef.current = map;

    map.on("error", (e) => {
      const msg = e.error?.message ?? String(e.error ?? "Map error");
      console.warn("[map]", msg, e);
      onOverlayErrorRef.current?.(msg);
    });

    const windHeat = new WindHeatmapLayer();
    const windArrows = new WindArrowLayer();
    windHeatRef.current = windHeat;
    windArrowsRef.current = windArrows;

    map.on("load", async () => {
      loadedRef.current = true;
      const empty = { type: "FeatureCollection" as const, features: [] };

      // Depth → land → wind → routes (forecast above chart, under route overlays)
      windHeat.attach(map);

      try {
        const arrowImg = await svgToImage(WIND_ARROW_SVG, 48);
        if (!map.hasImage(WIND_ARROW_ICON_ID)) {
          map.addImage(WIND_ARROW_ICON_ID, arrowImg, { pixelRatio: 2 });
        }
      } catch (e) {
        console.warn("[map] wind arrow icon failed to load", e);
      }
      windArrows.attach(map);

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
          "circle-radius": 6,
          "circle-color": ["match", ["get", "kind"], "start", "#4ade80", "finish", "#fb7185", "#facc15"],
          "circle-stroke-color": "#0b1220",
          "circle-stroke-width": 2,
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

      map.on("click", "routes", (e) => {
        const f = e.features?.[0];
        const mid = f?.properties?.model_id;
        if (mid && onRouteSelectRef.current) onRouteSelectRef.current(String(mid));
      });
      map.on("mouseenter", "routes", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "routes", () => {
        map.getCanvas().style.cursor = "";
      });

      // Wind under place labels under routes.
      try {
        if (map.getLayer("wind-heatmap-canvas") && map.getLayer("routes")) {
          map.moveLayer("wind-heatmap-canvas", "routes");
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
    });

    const debouncedViewport = debounce(() => {
      emitViewport();
      const grid = windGridRef.current;
      if (grid && propsRef.current.showWind) {
        windHeatRef.current?.syncViewport();
        windArrowsRef.current?.setGrid(grid, { immediate: true });
      }
    }, 350);

    map.on("moveend", debouncedViewport);
    map.on("zoomend", debouncedViewport);

    map.on("click", async (e) => {
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
      onViewportChangeRef.current?.(bbox, span);
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
      if (p.course && courseIsReady(p.course)) {
        courseFeatures.push({
          type: "Feature",
          properties: { kind: "start" },
          geometry: { type: "Point", coordinates: [p.course.start[1], p.course.start[0]] },
        });
        p.course.marks.forEach((mk, i) =>
          courseFeatures.push({
            type: "Feature",
            properties: { kind: "mark", label: mk.name ?? `M${i + 1}` },
            geometry: { type: "Point", coordinates: [mk.position[1], mk.position[0]] },
          }),
        );
        courseFeatures.push({
          type: "Feature",
          properties: { kind: "finish" },
          geometry: { type: "Point", coordinates: [p.course.finish[1], p.course.finish[0]] },
        });
      }
      (m.getSource("course") as maplibregl.GeoJSONSource | undefined)?.setData({
        type: "FeatureCollection",
        features: courseFeatures,
      });

      (m.getSource("playback-boat") as maplibregl.GeoJSONSource | undefined)?.setData(
        boatFeature(p.playbackPosition, p.playbackHeading),
      );

      windHeatRef.current?.setVisible(m, p.showWind && windDataRef.current);
      windArrowsRef.current?.setVisible(m, p.showWind && windDataRef.current);
    }

    syncLocalRef.current = () => syncLocalData();

    return () => {
      popupRef.current?.remove();
      windHeatRef.current?.destroy(map);
      windArrowsRef.current?.destroy(map);
      map.remove();
      mapRef.current = null;
      loadedRef.current = false;
    };
  }, []);

  useEffect(() => {
    syncLocalRef.current?.();
  }, [routes, course, playbackPosition, playbackHeading, showWind]);

  // Browse + playback: slice the series locally — no network.
  useEffect(() => {
    const m = mapRef.current;
    if (!m || !loadedRef.current) return;

    if (!showWind) {
      windDataRef.current = false;
      windGridRef.current = null;
      windHeatRef.current?.setGrid(null);
      windArrowsRef.current?.setGrid(null);
      syncLocalRef.current?.();
      return;
    }

    if (windSeries && windTimeUtc) {
      const grid = sliceSeries(windSeries, windTimeUtc);
      windDataRef.current = true;
      windGridRef.current = grid;
      windHeatRef.current?.setGrid(grid);
      windArrowsRef.current?.setGrid(grid, { immediate: true });
      syncLocalRef.current?.();
      return;
    }

    windDataRef.current = false;
    windGridRef.current = null;
    windHeatRef.current?.setGrid(null);
    windArrowsRef.current?.setGrid(null);
    syncLocalRef.current?.();
  }, [windSeries, windTimeUtc, showWind]);

  useEffect(() => {
    const m = mapRef.current;
    if (!m || !loadedRef.current || !course || !fitRouteKey || !courseIsReady(course)) return;
    m.fitBounds(
      [
        [Math.min(course.start[1], course.finish[1]) - 0.2, Math.min(course.start[0], course.finish[0]) - 0.2],
        [Math.max(course.start[1], course.finish[1]) + 0.2, Math.max(course.start[0], course.finish[0]) + 0.2],
      ],
      { padding: 48, duration: 800 },
    );
  }, [fitRouteKey, course]);

  return <div id="map" ref={containerRef} />;
}
