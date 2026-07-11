"use client";

import type { ModelPreset } from "@/lib/types";

const REGIONAL_IDS = new Set([
  "icon_eu",
  "icon_d2",
  "arome",
  "arpege_eu",
  "harmonie",
]);

export default function ModelPicker({
  models,
  selected,
  onToggle,
  onRun,
  running,
  forecastEnabled,
}: {
  models: ModelPreset[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onRun: () => void;
  running: boolean;
  forecastEnabled: boolean;
}) {
  const global = models.filter((m) => !REGIONAL_IDS.has(m.id));
  const regional = models.filter((m) => REGIONAL_IDS.has(m.id));

  const renderGroup = (title: string, list: ModelPreset[]) => (
    <>
      <div style={{ color: "var(--muted)", fontSize: 10, margin: "6px 0 4px", letterSpacing: "0.05em" }}>
        {title}
      </div>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {list.map((m) => (
          <li key={m.id} style={{ padding: "3px 0" }}>
            <label style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="checkbox"
                checked={selected.has(m.id)}
                onChange={() => onToggle(m.id)}
                disabled={!forecastEnabled}
              />
              <span className="swatch" style={{ background: m.color }} />
              {m.label}
              <span style={{ color: "var(--muted)", marginLeft: "auto", fontSize: 11 }}>
                {m.resolution_deg}°
              </span>
            </label>
          </li>
        ))}
      </ul>
    </>
  );

  return (
    <div>
      {!forecastEnabled ? (
        <div style={{ color: "var(--muted)", fontSize: 12, marginBottom: 8 }}>
          Zoom in below 300 mi to see available forecasts for this area.
        </div>
      ) : models.length === 0 ? (
        <div style={{ color: "var(--muted)" }}>No models available for this area.</div>
      ) : (
        <>
          {renderGroup("GLOBAL", global)}
          {regional.length > 0 && renderGroup("HIGH-RES REGIONAL", regional)}
        </>
      )}

      <button
        onClick={onRun}
        disabled={running || selected.size === 0 || !forecastEnabled}
        style={{
          width: "100%",
          marginTop: 8,
          background: running || selected.size === 0 || !forecastEnabled ? "var(--panel-2)" : "var(--accent)",
          color: running || selected.size === 0 || !forecastEnabled ? "var(--muted)" : "#04231f",
          border: "none",
          borderRadius: 6,
          padding: "7px 8px",
          fontWeight: 600,
          cursor: running || selected.size === 0 || !forecastEnabled ? "default" : "pointer",
        }}
      >
        {running ? "Routing…" : `Run ${selected.size || ""} model${selected.size === 1 ? "" : "s"}`}
      </button>
    </div>
  );
}
