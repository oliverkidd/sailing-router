"use client";

import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";
import { API_BASE } from "@/lib/api";
import { colorForModel } from "@/lib/colors";
import type { Course, ModelRoute } from "@/lib/types";

// UK + Mediterranean default view. OSM raster base (respect tile usage policy).
const UK_MED_CENTER: [number, number] = [12.5, 42.0]; // lon, lat (central Med)
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

async function addGeoJsonLayer(
  map: maplibregl.Map,
  url: string,
  id: string,
  layer: Omit<maplibregl.LayerSpecification, "id" | "source">,
) {
  try {
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.features?.length) return;
    if (map.getSource(id)) return;
    map.addSource(id, { type: "geojson", data });
    map.addLayer({ id, source: id, ...layer } as maplibregl.LayerSpecification);
  } catch {
    /* backend offline — overlays are optional */
  }
}

function routesToFeatureCollection(routes: ModelRoute[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes.map((r) => ({
      type: "Feature",
      properties: { model_id: r.model_id, color: colorForModel(r.model_id) },
      geometry: {
        type: "LineString",
        // GeoJSON is [lon, lat]; RouteLeg.position is [lat, lon].
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

export default function MapView({
  routes = [],
  course,
  showCurrents = false,
}: {
  routes?: ModelRoute[];
  course?: Course | null;
  showCurrents?: boolean;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const loadedRef = useRef(false);
  const syncRef = useRef<(() => void) | null>(null);
  const routesRef = useRef<ModelRoute[]>(routes);
  const courseRef = useRef<Course | null | undefined>(course);
  const showCurrentsRef = useRef<boolean>(showCurrents);
  routesRef.current = routes;
  courseRef.current = course;
  showCurrentsRef.current = showCurrents;

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

    map.on("load", () => {
      loadedRef.current = true;
      // Depth contours + coastline overlays (advisory). Fetched as GeoJSON;
      // the frontend never parses bathymetry grids itself.
      void addGeoJsonLayer(map, `${API_BASE}/land/mask`, "land", {
        type: "fill",
        paint: { "fill-color": "#1f2d1a", "fill-opacity": 0.75, "fill-outline-color": "#3d5a2e" },
      });
      void addGeoJsonLayer(map, `${API_BASE}/depth/contours`, "depth", {
        type: "line",
        paint: {
          "line-color": ["match", ["get", "depth_m"], 3, "#f87171", 10, "#38bdf8", "#64748b"],
          "line-width": 1.5,
          "line-dasharray": [2, 2],
        },
      });

      // Route + course sources (updated reactively below).
      map.addSource("routes", { type: "geojson", data: routesToFeatureCollection([]) });
      map.addLayer({
        id: "routes",
        source: "routes",
        type: "line",
        paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.9 },
      });
      map.addSource("maneuvers", { type: "geojson", data: maneuverPoints([]) });
      map.addLayer({
        id: "maneuvers",
        source: "maneuvers",
        type: "circle",
        paint: {
          "circle-radius": 3.5,
          "circle-color": ["get", "color"],
          "circle-stroke-color": "#0b1220",
          "circle-stroke-width": 1,
        },
      });
      map.addSource("currents", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "currents",
        source: "currents",
        type: "line",
        paint: {
          "line-color": ["interpolate", ["linear"], ["get", "rate_kn"], 0, "#93c5fd", 2, "#2563eb"],
          "line-width": 1.5,
        },
      });
      map.addSource("course", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "course-marks",
        source: "course",
        type: "circle",
        paint: {
          "circle-radius": 5,
          "circle-color": ["match", ["get", "kind"], "start", "#4ade80", "finish", "#fb7185", "#facc15"],
          "circle-stroke-color": "#0b1220",
          "circle-stroke-width": 2,
        },
      });
      syncData();
    });

    // Click -> elevation readout.
    map.on("click", async (e) => {
      try {
        const res = await fetch(
          `${API_BASE}/elevation?lat=${e.lngLat.lat.toFixed(4)}&lon=${e.lngLat.lng.toFixed(4)}`,
        );
        if (!res.ok) return;
        const { elevation_m } = await res.json();
        new maplibregl.Popup()
          .setLngLat(e.lngLat)
          .setHTML(
            `<div style="color:#111">${e.lngLat.lat.toFixed(3)}, ${e.lngLat.lng.toFixed(
              3,
            )}<br/>elev: ${elevation_m ?? "n/a"} m</div>`,
          )
          .addTo(map);
      } catch {
        /* backend offline */
      }
    });

    function syncData() {
      const m = mapRef.current;
      if (!m || !loadedRef.current) return;
      const routes = routesRef.current;
      const course = courseRef.current;
      (m.getSource("routes") as maplibregl.GeoJSONSource | undefined)?.setData(
        routesToFeatureCollection(routes),
      );
      (m.getSource("maneuvers") as maplibregl.GeoJSONSource | undefined)?.setData(
        maneuverPoints(routes),
      );
      const courseFeatures: GeoJSON.Feature[] = [];
      if (course) {
        courseFeatures.push({
          type: "Feature",
          properties: { kind: "start" },
          geometry: { type: "Point", coordinates: [course.start[1], course.start[0]] },
        });
        course.marks.forEach((mk, i) =>
          courseFeatures.push({
            type: "Feature",
            properties: { kind: "mark", label: mk.name ?? `M${i + 1}` },
            geometry: { type: "Point", coordinates: [mk.position[1], mk.position[0]] },
          }),
        );
        courseFeatures.push({
          type: "Feature",
          properties: { kind: "finish" },
          geometry: { type: "Point", coordinates: [course.finish[1], course.finish[0]] },
        });
      }
      (m.getSource("course") as maplibregl.GeoJSONSource | undefined)?.setData({
        type: "FeatureCollection",
        features: courseFeatures,
      });

      // Current arrows over the course bbox (advisory), when enabled.
      const cs = m.getSource("currents") as maplibregl.GeoJSONSource | undefined;
      if (cs) {
        if (showCurrentsRef.current && course) {
          const pts = [course.start, course.finish, ...course.marks.map((mk) => mk.position)];
          const lats = pts.map((p) => p[0]);
          const lons = pts.map((p) => p[1]);
          const q = new URLSearchParams({
            lat_min: String(Math.min(...lats) - 0.3),
            lon_min: String(Math.min(...lons) - 0.3),
            lat_max: String(Math.max(...lats) + 0.3),
            lon_max: String(Math.max(...lons) + 0.3),
            t_utc: course.depart_utc,
            source: "fes",
          });
          fetch(`${API_BASE}/currents/arrows?${q}`)
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => data && cs.setData(data))
            .catch(() => {});
        } else {
          cs.setData({ type: "FeatureCollection", features: [] });
        }
      }
    }

    syncRef.current = syncData;

    return () => {
      map.remove();
      mapRef.current = null;
      loadedRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-sync route/course/current sources whenever they change.
  useEffect(() => {
    syncRef.current?.();
  }, [routes, course, showCurrents]);

  return <div id="map" ref={containerRef} />;
}
