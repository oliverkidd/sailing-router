"use client";

import type { ModelPreset } from "@/lib/types";

export default function ModelPicker({
  models,
  selected,
  onToggle,
  useCurrents,
  useTerrain,
  onCurrents,
  onTerrain,
  onRun,
  running,
}: {
  models: ModelPreset[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  useCurrents: boolean;
  useTerrain: boolean;
  onCurrents: (v: boolean) => void;
  onTerrain: (v: boolean) => void;
  onRun: () => void;
  running: boolean;
}) {
  return (
    <div>
      {models.length === 0 ? (
        <div style={{ color: "var(--muted)" }}>No models loaded.</div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {models.map((m) => (
            <li key={m.id} style={{ padding: "3px 0" }}>
              <label style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={selected.has(m.id)}
                  onChange={() => onToggle(m.id)}
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
      )}

      <div style={{ display: "flex", gap: 12, margin: "8px 0", fontSize: 12 }}>
        <label style={{ color: "var(--muted)", cursor: "pointer" }}>
          <input type="checkbox" checked={useCurrents} onChange={(e) => onCurrents(e.target.checked)} />{" "}
          Currents
        </label>
        <label style={{ color: "var(--muted)", cursor: "pointer" }}>
          <input type="checkbox" checked={useTerrain} onChange={(e) => onTerrain(e.target.checked)} />{" "}
          Terrain wind
        </label>
      </div>

      <button
        onClick={onRun}
        disabled={running || selected.size === 0}
        style={{
          width: "100%",
          background: running || selected.size === 0 ? "var(--panel-2)" : "var(--accent)",
          color: running || selected.size === 0 ? "var(--muted)" : "#04231f",
          border: "none",
          borderRadius: 6,
          padding: "7px 8px",
          fontWeight: 600,
          cursor: running || selected.size === 0 ? "default" : "pointer",
        }}
      >
        {running ? "Routing…" : `Run ${selected.size || ""} model${selected.size === 1 ? "" : "s"}`}
      </button>
    </div>
  );
}
