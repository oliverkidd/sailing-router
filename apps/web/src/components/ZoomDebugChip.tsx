"use client";

import { useMemo } from "react";
import { describeZoomDebug } from "@/lib/zoomDebugInfo";

/** Temporary HUD — remove with zoomDebugInfo.ts when done fact-checking. */
export default function ZoomDebugChip({
  zoom,
  forecastZoom,
  terrainOn = true,
  onTerrainToggle,
  terrainDisabled = false,
  terrainLibrary,
  terrainApplied,
  terrainLibraryVersion,
}: {
  zoom: number;
  /** Synthetic /wind/area zoom (High def locks density off the camera). */
  forecastZoom?: number;
  terrainOn?: boolean;
  onTerrainToggle?: (on: boolean) => void;
  /** When true (e.g. routing active), toggle is shown but disabled. */
  terrainDisabled?: boolean;
  /** From last /wind/area: windninja | identity | none */
  terrainLibrary?: string | null;
  terrainApplied?: boolean | null;
  terrainLibraryVersion?: string | null;
}) {
  const { header, rows } = useMemo(
    () => describeZoomDebug(zoom, forecastZoom, terrainOn),
    [zoom, forecastZoom, terrainOn],
  );

  const lib = (terrainLibrary ?? "").toLowerCase() || null;
  const available = lib === "windninja" || lib === "identity";
  const statusLabel = !lib
    ? "WindNinja: …"
    : lib === "none"
      ? "WindNinja: unavailable"
      : lib === "identity"
        ? "WindNinja: identity (no-op)"
        : terrainOn && terrainApplied
          ? `WindNinja: on (${terrainLibraryVersion || lib})`
          : `WindNinja: off (${terrainLibraryVersion || lib})`;

  return (
    <div className="zoom-debug-chip">
      <div className="zoom-debug-chip-header">{header}</div>
      {rows.map((row) => (
        <div key={row.label} className="zoom-debug-chip-row">
          <span className="zoom-debug-chip-label">{row.label}</span>
          <span className="zoom-debug-chip-value">{row.value}</span>
        </div>
      ))}
      <div className="zoom-debug-chip-row">
        <span className="zoom-debug-chip-label">Terrain</span>
        <span className="zoom-debug-chip-value">{statusLabel}</span>
      </div>
      {onTerrainToggle && (
        <label
          className={`zoom-debug-chip-toggle${terrainDisabled ? " disabled" : ""}`}
          title={
            terrainDisabled
              ? "High def stays on during routing"
              : "High def: WindNinja + ~200 m grid at fetch time. Off: ~500 m, no WindNinja. Zooming in interpolates — it does not fetch more detail."
          }
        >
          <input
            type="checkbox"
            checked={terrainOn}
            disabled={terrainDisabled}
            onChange={(e) => onTerrainToggle(e.target.checked)}
          />
          <span>High def</span>
        </label>
      )}
    </div>
  );
}
