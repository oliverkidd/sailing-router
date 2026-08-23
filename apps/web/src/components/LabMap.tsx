"use client";

import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";
import { buildChartStyle, ensurePmtilesProtocol } from "@/components/MapView";
import { coursePoints } from "@/lib/labCourses";
import { REGION_MAX_BOUNDS } from "@/lib/region";
import type { Course, RouteLeg } from "@/lib/types";

export interface LabMapRoute {
  id: string;
  color: string;
  legs: RouteLeg[];
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

function routesCollection(routes: LabMapRoute[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes
      .filter((r) => r.legs.length >= 2)
      .map((r) => ({
        type: "Feature" as const,
        properties: { id: r.id, color: r.color },
        geometry: {
          type: "LineString" as const,
          coordinates: r.legs.map((leg) => [leg.position[1], leg.position[0]]),
        },
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
}: {
  course: Course;
  routes: LabMapRoute[];
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const loadedRef = useRef(false);
  const courseRef = useRef(course);
  const routesRef = useRef(routes);
  courseRef.current = course;
  routesRef.current = routes;

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

    map.on("load", () => {
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
            "line-width": 2.6,
            "line-opacity": 0.9,
          },
        },
        "lab-course-points",
      );
      fitCourse(map, courseRef.current);
    });

    const ro = new ResizeObserver(() => map.resize());
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      loadedRef.current = false;
      map.remove();
      mapRef.current = null;
    };
    // Map is created once; course/routes sync in the effects below.
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
  }, [routes]);

  return <div className="lab-map" ref={containerRef} />;
}
