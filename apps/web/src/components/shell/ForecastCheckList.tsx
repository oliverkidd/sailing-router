"use client";

import type { ModelPreset } from "@/lib/types";

const REGIONAL_IDS = new Set([
  "icon_eu",
  "icon_d2",
  "arome",
  "arpege_eu",
  "harmonie",
]);

const GLOBAL_TIP =
  "Global model — worldwide coverage, coarser grid. Best for long passages and areas without a regional nest.";
const REGIONAL_TIP =
  "High-res regional model — finer grid over a limited area. Better coastal detail when you are inside its domain.";

function GlobeIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M3.5 12h17M12 3c2.5 2.8 3.8 5.8 3.8 9s-1.3 6.2-3.8 9c-2.5-2.8-3.8-5.8-3.8-9s1.3-6.2 3.8-9z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function HiResIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M4 18l5-7 3.5 4.5L16 11l4 7H4z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <circle cx="9" cy="7" r="1.6" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export default function ForecastCheckList({
  models,
  selected,
  onToggle,
  apiUp,
  hasViewport,
}: {
  models: ModelPreset[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  apiUp?: boolean | null;
  hasViewport?: boolean;
}) {
  if (models.length === 0) {
    return (
      <p className="forecast-checklist-empty">
        {apiUp === false
          ? "Forecast API unavailable — try restarting the dev server."
          : !hasViewport
            ? "Pan the map to see available forecasts."
            : "No forecasts available for this area."}
      </p>
    );
  }

  return (
    <div className="forecast-checklist">
      {models.map((m) => {
        const regional = REGIONAL_IDS.has(m.id);
        return (
          <label key={m.id} className="forecast-checklist-item">
            <input
              type="checkbox"
              checked={selected.has(m.id)}
              onChange={() => onToggle(m.id)}
            />
            <span className="swatch" style={{ background: m.color }} />
            <span className="forecast-checklist-label">{m.label}</span>
            <span className="forecast-checklist-res">{m.resolution_deg}°</span>
            <span
              className={`forecast-checklist-kind${regional ? " regional" : " global"}`}
              title={regional ? REGIONAL_TIP : GLOBAL_TIP}
              aria-label={regional ? "High-res regional model" : "Global model"}
            >
              {regional ? <HiResIcon /> : <GlobeIcon />}
            </span>
          </label>
        );
      })}
    </div>
  );
}
