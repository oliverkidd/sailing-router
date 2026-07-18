"use client";

import { useMemo } from "react";
import { describeZoomDebug } from "@/lib/zoomDebugInfo";

/** Temporary HUD — remove with zoomDebugInfo.ts when done fact-checking. */
export default function ZoomDebugChip({
  zoom,
  terrainOn = true,
  onTerrainToggle,
  terrainDisabled = false,
  terrainLibrary,
  terrainApplied,
  terrainLibraryVersion,
}: {
  zoom: number;
  terrainOn?: boolean;
  onTerrainToggle?: (on: boolean) => void;
  /** When true (e.g. routing active), toggle is shown but disabled. */
  terrainDisabled?: boolean;
  /** From last /wind/area: windninja | identity | none */
  terrainLibrary?: string | null;
  terrainApplied?: boolean | null;
  terrainLibraryVersion?: string | null;
}) {
  const { header, rows } = useMemo(() => describeZoomDebug(zoom), [zoom]);

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
    <div className="zoom-debug-chip" aria-hidden>
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
          className={`zoom-debug-chip-toggle${terrainDisabled || !available ? " disabled" : ""}`}
          title={
            terrainDisabled
              ? "WindNinja opt stays on during routing"
              : !available
                ? "No terrain library baked — run build-data --cyclades"
                : "Toggle terrain downscaling on the browse forecast"
          }
        >
          <input
            type="checkbox"
            checked={terrainOn}
            disabled={terrainDisabled || !available}
            onChange={(e) => onTerrainToggle(e.target.checked)}
          />
          <span>WindNinja opt</span>
        </label>
      )}
    </div>
  );
}
