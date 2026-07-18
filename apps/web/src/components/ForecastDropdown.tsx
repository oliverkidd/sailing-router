"use client";

import { useEffect, useRef, useState } from "react";
import type { ModelPreset } from "@/lib/types";

const REGIONAL_IDS = new Set([
  "icon_eu",
  "icon_d2",
  "arome",
  "arpege_eu",
  "harmonie",
]);

export default function ForecastDropdown({
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
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const global = models.filter((m) => !REGIONAL_IDS.has(m.id));
  const regional = models.filter((m) => REGIONAL_IDS.has(m.id));
  const label =
    selected.size === 0
      ? "Choose forecasts"
      : `${selected.size} forecast${selected.size === 1 ? "" : "s"}`;

  const renderGroup = (title: string, list: ModelPreset[]) => {
    if (list.length === 0) return null;
    return (
      <div key={title} className="forecast-dropdown-group">
        <div className="forecast-dropdown-group-title">{title}</div>
        {list.map((m) => (
          <label key={m.id} className="forecast-dropdown-item">
            <input
              type="checkbox"
              checked={selected.has(m.id)}
              onChange={() => onToggle(m.id)}
            />
            <span className="swatch" style={{ background: m.color }} />
            <span>{m.label}</span>
            <span className="forecast-dropdown-res">{m.resolution_deg}°</span>
          </label>
        ))}
      </div>
    );
  };

  return (
    <div className="forecast-dropdown" ref={rootRef}>
      <button
        type="button"
        className="forecast-dropdown-trigger"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        {label}
        <span aria-hidden style={{ opacity: 0.7, marginLeft: 6 }}>
          {open ? "▴" : "▾"}
        </span>
      </button>
      {open && (
        <div className="forecast-dropdown-menu" role="listbox">
          {models.length === 0 ? (
            <div className="forecast-dropdown-empty">
              {apiUp === false
                ? "Forecast API unavailable — try restarting the dev server."
                : !hasViewport
                  ? "Zoom to an area to see available forecasts."
                  : "No forecasts available for this area."}
            </div>
          ) : (
            <>
              {renderGroup("GLOBAL", global)}
              {renderGroup("HIGH-RES REGIONAL", regional)}
            </>
          )}
        </div>
      )}
    </div>
  );
}
