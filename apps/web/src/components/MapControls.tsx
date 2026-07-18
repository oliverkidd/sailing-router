"use client";

export interface OverlayToggles {
  showWind: boolean;
  showDepth: boolean;
}

export default function MapControls({
  toggles,
  onChange,
}: {
  toggles: OverlayToggles;
  onChange: (patch: Partial<OverlayToggles>) => void;
}) {
  const btn = (active: boolean) =>
    ({
      background: active ? "var(--accent)" : "rgba(17, 26, 43, 0.92)",
      color: active ? "#04231f" : "var(--text)",
      border: "1px solid var(--border)",
      borderRadius: 6,
      padding: "6px 10px",
      fontSize: 11,
      fontWeight: active ? 600 : 400,
      cursor: "pointer",
      backdropFilter: "blur(6px)",
    }) as const;

  return (
    <div className="map-controls">
      <button type="button" style={btn(toggles.showWind)} onClick={() => onChange({ showWind: !toggles.showWind })}>
        Show wind
      </button>
      <button type="button" style={btn(toggles.showDepth)} onClick={() => onChange({ showDepth: !toggles.showDepth })}>
        Show depth
      </button>
    </div>
  );
}
