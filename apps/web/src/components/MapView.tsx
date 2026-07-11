"use client";

import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";
import { api } from "@/lib/api";
import {
  boundsFromMap,
  courseIsReady,
  type MapBounds,
} from "@/lib/mapOverlays";
import { sampleGrid, type MeteoGrid } from "@/lib/meteoGrid";
import { WindHeatmapLayer } from "@/lib/windHeatmap";
import { WindArrowLayer, WIND_ARROW_ICON_ID } from "@/lib/windArrows";
import { colorForModel } from "@/lib/colors";
import { BOAT_SVG, WIND_ARROW_SVG, svgToImage } from "@/lib/mapIcons";
import { formatPointPopup, type PointInspect } from "@/lib/mapPopup";
import {
  ensureWindTiles,
  stitchTiles,
  viewportMaxSpanMiles,
} from "@/lib/windTiles";
import type { Course, ModelRoute } from "@/lib/types";

const UK_MED_CENTER: [number, number] = [12.5, 42.0];
const DEFAULT_ZOOM = 4;

const OSM_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [
    { id: "background", type: "background", paint: { "background-color": "#0b1220" } },
    { id: "osm", type: "raster", source: "osm", paint: { "raster-opacity": 0.85 } },
  ],
};

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
  forecastEnabled = false,
  windModelId = "ecmwf",
  windModelResolutionDeg = 0.25,
  windTimeUtc,
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
  /** True when viewport max span < 300 mi — gates wind tile fetches. */
  forecastEnabled?: boolean;
  windModelId?: string;
  windModelResolutionDeg?: number;
  windTimeUtc?: string;
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
  const syncOverlaysRef = useRef<(() => void) | null>(null);
  const abortRef = useRef<AbortController | null>(null);
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
    forecastEnabled,
    windModelId,
    windModelResolutionDeg,
    windTimeUtc,
    playbackPosition,
    playbackHeading,
  });

  propsRef.current = {
    routes,
    course,
    showWind,
    forecastEnabled,
    windModelId,
    windModelResolutionDeg,
    windTimeUtc,
    playbackPosition,
    playbackHeading,
  };
  onRouteSelectRef.current = onRouteSelect;
  onMapClickRef.current = onMapClick;
  onOverlayErrorRef.current = onOverlayError;
  onViewportChangeRef.current = onViewportChange;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: OSM_STYLE,
      center: UK_MED_CENTER,
      zoom: DEFAULT_ZOOM,
      maxZoom: 14,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "nautical" as never }), "bottom-right");
    mapRef.current = map;

    const windHeat = new WindHeatmapLayer();
    const windArrows = new WindArrowLayer();
    windHeatRef.current = windHeat;
    windArrowsRef.current = windArrows;

    map.on("load", async () => {
      loadedRef.current = true;
      const empty = { type: "FeatureCollection" as const, features: [] };

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

      map.moveLayer("wind-heatmap-canvas", "routes");
      if (map.getLayer("wind-arrows")) map.moveLayer("wind-arrows", "routes");

      emitViewport();
      syncLocalData();
      syncOverlays();
    });

    const debouncedViewport = debounce(() => {
      emitViewport();
      // Re-render arrows for new screen positions; tiles only fetch if missing.
      const grid = windGridRef.current;
      if (grid && propsRef.current.showWind) {
        windArrowsRef.current?.setGrid(grid);
      }
      syncOverlays();
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
      popupRef.current = new maplibregl.Popup({ maxWidth: "240px" })
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

    async function syncOverlays() {
      const m = mapRef.current;
      if (!m || !loadedRef.current) return;
      const p = propsRef.current;
      const bbox: MapBounds = boundsFromMap(() => m.getBounds());
      const tUtc = p.windTimeUtc ?? p.course?.depart_utc ?? new Date().toISOString();

      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;

      if (!p.showWind || !p.forecastEnabled) {
        windDataRef.current = false;
        windGridRef.current = null;
        windHeatRef.current?.setGrid(null);
        windArrowsRef.current?.setGrid(null);
        syncLocalData();
        onOverlayErrorRef.current?.(null);
        return;
      }

      try {
        const { tiles, error } = await ensureWindTiles({
          viewport: bbox,
          modelId: p.windModelId,
          modelResolutionDeg: p.windModelResolutionDeg,
          tUtc,
          signal: ac.signal,
        });
        if (ac.signal.aborted) return;

        const stitched = stitchTiles(tiles);
        if (stitched) {
          windDataRef.current = true;
          windGridRef.current = stitched;
          windHeatRef.current?.setGrid(stitched);
          windArrowsRef.current?.setGrid(stitched);
          onOverlayErrorRef.current?.(error ? `Wind: ${error}` : null);
        } else {
          windDataRef.current = false;
          windGridRef.current = null;
          windHeatRef.current?.setGrid(null);
          windArrowsRef.current?.setGrid(null);
          onOverlayErrorRef.current?.(error ? `Wind: ${error}` : "Wind unavailable");
        }
        syncLocalData();
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        windDataRef.current = false;
        windGridRef.current = null;
        windHeatRef.current?.setGrid(null);
        windArrowsRef.current?.setGrid(null);
        onOverlayErrorRef.current?.(`Wind: ${String(e)}`);
        syncLocalData();
      }
    }

    syncLocalRef.current = () => syncLocalData();
    syncOverlaysRef.current = () => void syncOverlays();

    return () => {
      abortRef.current?.abort();
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

  useEffect(() => {
    syncOverlaysRef.current?.();
  }, [showWind, forecastEnabled, windModelId, windModelResolutionDeg, windTimeUtc]);

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
