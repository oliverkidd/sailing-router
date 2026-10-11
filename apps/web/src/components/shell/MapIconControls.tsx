"use client";

import type { OverlayToggles } from "@/components/MapControls";

function WindIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M4 8h10a2.5 2.5 0 1 0-2.5-2.5M4 12h14a2.5 2.5 0 1 1-2.5 2.5M4 16h8a2.5 2.5 0 1 1-2.5 2.5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function DepthIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M3 7c3 0 3 3 6 3s3-3 6-3 3 3 6 3M3 12c3 0 3 3 6 3s3-3 6-3 3 3 6 3M3 17c3 0 3 3 6 3s3-3 6-3 3 3 6 3"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function TerrainIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M3 18l5.5-8 4 5 2.5-3.5L21 18H3z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      <path d="M14 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4z" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function GpsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.8" />
      <path
        d="M12 3v3M12 18v3M3 12h3M18 12h3"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <circle cx="12" cy="12" r="7" stroke="currentColor" strokeWidth="1.4" opacity="0.55" />
    </svg>
  );
}

export default function MapIconControls({
  toggles,
  terrain = false,
  terrainDisabled = false,
  onTerrain,
  gpsOn = false,
  onGpsChange,
  onChange,
}: {
  toggles: OverlayToggles;
  terrain?: boolean;
  terrainDisabled?: boolean;
  onTerrain?: (on: boolean) => void;
  gpsOn?: boolean;
  onGpsChange?: (on: boolean) => void;
  onChange: (patch: Partial<OverlayToggles>) => void;
}) {
  return (
    <div className="map-chrome-top-right">
      {onGpsChange && (
        <button
          type="button"
          className={`map-icon-btn map-gps-chip${gpsOn ? " active" : ""}`}
          aria-pressed={gpsOn}
          title={gpsOn ? "Hide GPS location" : "Show GPS location"}
          onClick={() => onGpsChange(!gpsOn)}
        >
          <GpsIcon />
          <span className="map-icon-btn-label">GPS</span>
        </button>
      )}
      <div className="map-icon-toggles" role="group" aria-label="Map overlays">
        <button
          type="button"
          className={`map-icon-btn${toggles.showWind ? " active" : ""}`}
          aria-pressed={toggles.showWind}
          title="Show wind"
          onClick={() => onChange({ showWind: !toggles.showWind })}
        >
          <WindIcon />
          <span className="map-icon-btn-label">Wind</span>
        </button>
        <button
          type="button"
          className={`map-icon-btn${toggles.showDepth ? " active" : ""}`}
          aria-pressed={toggles.showDepth}
          title="Show depth"
          onClick={() => onChange({ showDepth: !toggles.showDepth })}
        >
          <DepthIcon />
          <span className="map-icon-btn-label">Depth</span>
        </button>
        {onTerrain && (
          <button
            type="button"
            className={`map-icon-btn${terrain ? " active" : ""}`}
            aria-pressed={terrain}
            disabled={terrainDisabled}
            title={
              terrainDisabled
                ? "Terrain locked"
                : "Terrain: WindNinja + ~200 m grid (browse + routing). Off: ~500 m, raw forecast."
            }
            onClick={() => onTerrain(!terrain)}
          >
            <TerrainIcon />
            <span className="map-icon-btn-label">Terrain</span>
          </button>
        )}
      </div>
    </div>
  );
}
