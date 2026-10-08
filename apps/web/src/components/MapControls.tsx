"use client";

export interface OverlayToggles {
  showWind: boolean;
  showDepth: boolean;
}

export default function MapControls({
  toggles,
  highDef = false,
  highDefDisabled = false,
  onHighDef,
  onChange,
}: {
  toggles: OverlayToggles;
  highDef?: boolean;
  highDefDisabled?: boolean;
  onHighDef?: (on: boolean) => void;
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
      {onHighDef && (
        <button
          type="button"
          style={btn(highDef)}
          disabled={highDefDisabled}
          title={
            highDefDisabled
              ? "High def stays on during routing"
              : "High def: WindNinja + ~200 m grid at fetch time. Off: ~500 m, no WindNinja. Zooming in interpolates — it does not fetch more detail."
          }
          onClick={() => onHighDef(!highDef)}
        >
          High def
        </button>
      )}
    </div>
  );
}
