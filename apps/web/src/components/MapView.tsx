"use client";

import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";
import { API_BASE } from "@/lib/api";

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

export default function MapView() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

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

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  return <div id="map" ref={containerRef} />;
}
