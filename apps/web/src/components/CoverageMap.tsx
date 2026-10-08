"use client";

import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";
import { buildChartStyle, ensurePmtilesProtocol } from "@/components/MapView";
import {
  COVERAGE_BOXES,
  boxRing,
  formatBoxEdges,
  type CoverageBox,
} from "@/lib/coverageBoxes";
import { REGION_BBOX, REGION_CENTER } from "@/lib/region";

const CURRENT_RING = boxRing({
  lonMin: REGION_BBOX.lon_min,
  latMin: REGION_BBOX.lat_min,
  lonMax: REGION_BBOX.lon_max,
  latMax: REGION_BBOX.lat_max,
});

function coverageCollection(): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: COVERAGE_BOXES.map((box) => ({
      type: "Feature",
      properties: { id: box.id, name: box.name, color: box.color },
      geometry: { type: "Polygon", coordinates: [boxRing(box)] },
    })),
  };
}

function currentCollection(): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { name: "On disk now" },
        geometry: { type: "Polygon", coordinates: [CURRENT_RING] },
      },
    ],
  };
}

const FIT_PADDING = { top: 36, right: 36, bottom: 36, left: 320 };

function fitBox(map: maplibregl.Map, box: CoverageBox) {
  map.fitBounds(
    [
      [box.lonMin, box.latMin],
      [box.lonMax, box.latMax],
    ],
    { padding: FIT_PADDING, duration: 600 },
  );
}

function fitAll(map: maplibregl.Map) {
  const bounds = new maplibregl.LngLatBounds();
  for (const box of COVERAGE_BOXES) {
    bounds.extend([box.lonMin, box.latMin]);
    bounds.extend([box.lonMax, box.latMax]);
  }
  map.fitBounds(bounds, { padding: FIT_PADDING, duration: 0 });
}

export default function CoverageMap() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    ensurePmtilesProtocol();
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: buildChartStyle(window.location.origin),
      center: REGION_CENTER,
      zoom: 3.2,
      minZoom: 2,
      maxZoom: 16,
      maxPitch: 0,
      maxBounds: [
        [REGION_BBOX.lon_min - 8, REGION_BBOX.lat_min - 8],
        [REGION_BBOX.lon_max + 8, REGION_BBOX.lat_max + 8],
      ],
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
      fadeDuration: 0,
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;

    map.on("load", () => {
      map.addSource("coverage-current", { type: "geojson", data: currentCollection() });
      map.addLayer({
        id: "coverage-current-line",
        type: "line",
        source: "coverage-current",
        paint: {
          "line-color": "#334155",
          "line-width": 1.5,
          "line-dasharray": [2, 2],
          "line-opacity": 0.85,
        },
      });

      map.addSource("coverage-boxes", { type: "geojson", data: coverageCollection() });
      map.addLayer({
        id: "coverage-fill",
        type: "fill",
        source: "coverage-boxes",
        paint: {
          "fill-color": ["get", "color"],
          "fill-opacity": 0.28,
        },
      });
      map.addLayer({
        id: "coverage-line",
        type: "line",
        source: "coverage-boxes",
        paint: {
          "line-color": ["get", "color"],
          "line-width": 2,
        },
      });
      map.addLayer({
        id: "coverage-label",
        type: "symbol",
        source: "coverage-boxes",
        layout: {
          "text-field": ["get", "name"],
          "text-size": 13,
          "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
          "text-allow-overlap": true,
        },
        paint: {
          "text-color": "#0b1220",
          "text-halo-color": "#ffffff",
          "text-halo-width": 1.5,
        },
      });

      fitAll(map);
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  return (
    <div className="coverage-shell">
      <div ref={containerRef} className="coverage-map" />
      <aside className="coverage-legend">
        <p className="coverage-kicker">Throwaway sign-off</p>
        <h1>Chart to keep</h1>
        <p className="coverage-note">
          Colour is the water we would keep. The dashed line is everything baked on disk today.
          South of Gabes is out, as are Scandinavia, the Baltic, and the North Sea past Dover.
        </p>
        <ul>
          {COVERAGE_BOXES.map((box) => (
            <li key={box.id}>
              <button type="button" onClick={() => mapRef.current && fitBox(mapRef.current, box)}>
                <span className="coverage-swatch" style={{ background: box.color }} />
                <span>
                  <strong>{box.name}</strong>
                  <em>{formatBoxEdges(box)}</em>
                  <em>{box.blurb}</em>
                </span>
              </button>
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="coverage-all"
          onClick={() => mapRef.current && fitAll(mapRef.current)}
        >
          Show all
        </button>
      </aside>
    </div>
  );
}
