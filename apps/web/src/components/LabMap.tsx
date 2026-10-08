"use client";

import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";
import { buildChartStyle, ensurePmtilesProtocol } from "@/components/MapView";
import { coursePoints } from "@/lib/labCourses";
import { BOAT_SVG, WIND_ARROW_SVG, svgToImage } from "@/lib/mapIcons";
import type { MeteoGrid } from "@/lib/meteoGrid";
import { REGION_MAX_BOUNDS } from "@/lib/region";
import type { Course, LatLon, RouteLeg } from "@/lib/types";
import { ARROW_PEAK_OPACITY, WIND_ARROW_ICON_ID, WindArrowLayer } from "@/lib/windArrows";

const BOAT_ICON_ID = "lab-boat";
/** Sparser than the main map — six 240 px cards must stay interactive. */
const LAB_ARROW_SPACING_PX = 60;

export interface LabPlayhead {
  position: LatLon;
  heading: number;
  /** Index of the last leg at or before the playhead. */
  index: number;
}

export interface LabMapRoute {
  id: string;
  color: string;
  legs: RouteLeg[];
  playhead?: LabPlayhead | null;
  /** Advisory headland chain, when the run had waypoint guidance on. */
  waypoints?: LatLon[] | null;
}

function courseCollection(course: Course): GeoJSON.FeatureCollection {
  const pts = coursePoints(course);
  const features: GeoJSON.Feature[] = [];
  if (pts.length >= 2) {
    features.push({
      type: "Feature",
      properties: { kind: "axis" },
      geometry: {
        type: "LineString",
        coordinates: pts.map(([lat, lon]) => [lon, lat]),
      },
    });
  }
  features.push({
    type: "Feature",
    properties: { kind: "start", label: "S" },
    geometry: { type: "Point", coordinates: [course.start[1], course.start[0]] },
  });
  course.marks.forEach((mk, i) => {
    features.push({
      type: "Feature",
      properties: { kind: "mark", label: String(i + 1) },
      geometry: { type: "Point", coordinates: [mk.position[1], mk.position[0]] },
    });
  });
  features.push({
    type: "Feature",
    properties: { kind: "finish", label: "F" },
    geometry: { type: "Point", coordinates: [course.finish[1], course.finish[0]] },
  });
  return { type: "FeatureCollection", features };
}

const lngLat = (p: LatLon): [number, number] => [p[1], p[0]];

/** Split each route at its playhead so the track ahead of the boat can dim. */
function routesCollection(routes: LabMapRoute[]): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  for (const r of routes) {
    if (r.legs.length < 2) continue;
    const coords = r.legs.map((leg) => lngLat(leg.position));
    const head = r.playhead;
    if (!head) {
      features.push({
        type: "Feature",
        properties: { id: r.id, color: r.color, sailed: 1 },
        geometry: { type: "LineString", coordinates: coords },
      });
      continue;
    }
    const cut = Math.max(0, Math.min(r.legs.length - 1, head.index));
    const boat = lngLat(head.position);
    const sailed = [...coords.slice(0, cut + 1), boat];
    const ahead = [boat, ...coords.slice(cut + 1)];
    if (sailed.length >= 2) {
      features.push({
        type: "Feature",
        properties: { id: r.id, color: r.color, sailed: 1 },
        geometry: { type: "LineString", coordinates: sailed },
      });
    }
    if (ahead.length >= 2) {
      features.push({
        type: "Feature",
        properties: { id: r.id, color: r.color, sailed: 0 },
        geometry: { type: "LineString", coordinates: ahead },
      });
    }
  }
  return { type: "FeatureCollection", features };
}

/** Hollow rings in the run colour — a bad chain must be distinguishable from a
 *  bad commit policy, and these are advisory, not marks the boat has to enter. */
function waypointsCollection(routes: LabMapRoute[]): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  for (const r of routes) {
    (r.waypoints ?? []).forEach((p, i) => {
      features.push({
        type: "Feature",
        properties: { id: r.id, color: r.color, label: String(i + 1) },
        geometry: { type: "Point", coordinates: lngLat(p) },
      });
    });
  }
  return { type: "FeatureCollection", features };
}

function boatsCollection(routes: LabMapRoute[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes
      .filter((r) => r.playhead != null)
      .map((r) => ({
        type: "Feature" as const,
        properties: { id: r.id, color: r.color, heading: r.playhead!.heading },
        geometry: { type: "Point" as const, coordinates: lngLat(r.playhead!.position) },
      })),
  };
}

function fitCourse(map: maplibregl.Map, course: Course) {
  const pts = coursePoints(course);
  if (pts.length === 0) return;
  const b = new maplibregl.LngLatBounds();
  for (const [lat, lon] of pts) b.extend([lon, lat]);
  if (b.isEmpty()) return;
  map.fitBounds(b, { padding: 36, maxZoom: 13, duration: 0 });
}

export default function LabMap({
  course,
  routes,
  windGrid,
}: {
  course: Course;
  routes: LabMapRoute[];
  windGrid?: MeteoGrid | null;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const arrowsRef = useRef<WindArrowLayer | null>(null);
  const loadedRef = useRef(false);
  const courseRef = useRef(course);
  const routesRef = useRef(routes);
  const windRef = useRef(windGrid);
  courseRef.current = course;
  routesRef.current = routes;
  windRef.current = windGrid;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    ensurePmtilesProtocol();
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: buildChartStyle(window.location.origin),
      center: [course.start[1], course.start[0]],
      zoom: 11,
      minZoom: 6,
      maxZoom: 16,
      maxPitch: 0,
      maxBounds: REGION_MAX_BOUNDS,
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
      fadeDuration: 0,
    });
    map.scrollZoom.disable();
    map.touchZoomRotate.disableRotation();
    mapRef.current = map;
    const arrows = new WindArrowLayer();
    arrowsRef.current = arrows;

    map.on("load", async () => {
      loadedRef.current = true;
      map.addSource("lab-course", { type: "geojson", data: courseCollection(courseRef.current) });
      map.addLayer({
        id: "lab-course-line",
        type: "line",
        source: "lab-course",
        filter: ["==", ["get", "kind"], "axis"],
        paint: {
          "line-color": "#64748b",
          "line-width": 1.5,
          "line-dasharray": [2, 2],
          "line-opacity": 0.7,
        },
      });
      map.addLayer({
        id: "lab-course-points",
        type: "circle",
        source: "lab-course",
        filter: ["!=", ["get", "kind"], "axis"],
        paint: {
          "circle-radius": 7,
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
          "circle-stroke-width": 1.5,
        },
      });
      map.addLayer({
        id: "lab-course-labels",
        type: "symbol",
        source: "lab-course",
        filter: ["!=", ["get", "kind"], "axis"],
        layout: {
          "text-field": ["get", "label"],
          "text-size": 10,
          "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
          "text-allow-overlap": true,
          "text-ignore-placement": true,
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
      map.addSource("lab-routes", { type: "geojson", data: routesCollection(routesRef.current) });
      map.addLayer(
        {
          id: "lab-routes",
          type: "line",
          source: "lab-routes",
          paint: {
            "line-color": ["get", "color"],
            "line-width": ["case", ["==", ["get", "sailed"], 1], 2.6, 1.8],
            "line-opacity": ["case", ["==", ["get", "sailed"], 1], 0.95, 0.3],
          },
        },
        "lab-course-points",
      );
      map.addSource("lab-waypoints", {
        type: "geojson",
        data: waypointsCollection(routesRef.current),
      });
      map.addLayer(
        {
          id: "lab-waypoints",
          type: "circle",
          source: "lab-waypoints",
          paint: {
            "circle-radius": 4.5,
            "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-color": ["get", "color"],
            "circle-stroke-width": 1.6,
            "circle-stroke-opacity": 0.9,
          },
        },
        "lab-course-points",
      );

      // Wind under the route overlays, same ordering as the main chart.
      try {
        const arrowImg = await svgToImage(WIND_ARROW_SVG, 48);
        if (!map.hasImage(WIND_ARROW_ICON_ID)) {
          map.addImage(WIND_ARROW_ICON_ID, arrowImg, { pixelRatio: 2 });
        }
      } catch (e) {
        console.warn("[lab] wind arrow icon failed to load", e);
      }
      arrows.setStyle({ spacingPx: LAB_ARROW_SPACING_PX, peakOpacity: ARROW_PEAK_OPACITY });
      arrows.attach(map, "lab-routes");
      if (windRef.current) arrows.setGrid(windRef.current, { immediate: true });

      try {
        const boatImg = await svgToImage(BOAT_SVG, 60);
        if (!map.hasImage(BOAT_ICON_ID)) {
          map.addImage(BOAT_ICON_ID, boatImg, { pixelRatio: 3 });
        }
      } catch (e) {
        console.warn("[lab] boat icon failed to load", e);
      }
      map.addSource("lab-boats", { type: "geojson", data: boatsCollection(routesRef.current) });
      map.addLayer({
        id: "lab-boats-halo",
        type: "circle",
        source: "lab-boats",
        paint: {
          "circle-radius": 9,
          "circle-color": ["get", "color"],
          "circle-opacity": 0.35,
          "circle-stroke-color": ["get", "color"],
          "circle-stroke-width": 1.5,
        },
      });
      map.addLayer({
        id: "lab-boats",
        type: "symbol",
        source: "lab-boats",
        layout: {
          "icon-image": BOAT_ICON_ID,
          "icon-size": 0.32,
          "icon-rotate": ["get", "heading"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });

      fitCourse(map, courseRef.current);
    });

    const ro = new ResizeObserver(() => map.resize());
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      loadedRef.current = false;
      arrows.destroy(map);
      arrowsRef.current = null;
      map.remove();
      mapRef.current = null;
    };
    // Map is created once; course/routes/wind sync in the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loadedRef.current) return;
    (map.getSource("lab-course") as maplibregl.GeoJSONSource | undefined)?.setData(
      courseCollection(course),
    );
    fitCourse(map, course);
  }, [course]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loadedRef.current) return;
    (map.getSource("lab-routes") as maplibregl.GeoJSONSource | undefined)?.setData(
      routesCollection(routes),
    );
    (map.getSource("lab-boats") as maplibregl.GeoJSONSource | undefined)?.setData(
      boatsCollection(routes),
    );
    (map.getSource("lab-waypoints") as maplibregl.GeoJSONSource | undefined)?.setData(
      waypointsCollection(routes),
    );
  }, [routes]);

  useEffect(() => {
    if (!loadedRef.current) return;
    arrowsRef.current?.setGrid(windGrid ?? null, { immediate: true });
  }, [windGrid]);

  return <div className="lab-map" ref={containerRef} />;
}
