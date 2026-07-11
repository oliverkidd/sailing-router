"use client";

import { colorForModel } from "@/lib/colors";
import type { ModelPreset, ModelRoute } from "@/lib/types";

export default function ForecastPicker({
  routes,
  models,
  selectedModels,
  activeModelId,
  onActiveModel,
  enabled,
}: {
  routes: ModelRoute[];
  models: ModelPreset[];
  selectedModels: Set<string>;
  activeModelId: string;
  onActiveModel: (id: string) => void;
  enabled: boolean;
}) {
  if (!enabled) {
    return (
      <div className="forecast-picker">
        <span
          style={{
            background: "rgba(17, 26, 43, 0.92)",
            color: "var(--muted)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "5px 10px",
            fontSize: 11,
            backdropFilter: "blur(6px)",
          }}
        >
          Zoom in to choose a forecast
        </span>
      </div>
    );
  }

  const items =
    routes.length > 0
      ? routes.map((r) => ({ id: r.model_id, label: r.model_id }))
      : Array.from(selectedModels).map((id) => {
          const m = models.find((x) => x.id === id);
          return { id, label: m?.label ?? id };
        });

  if (items.length === 0) return null;

  const btn = (active: boolean, color: string) =>
    ({
      background: active ? color : "rgba(17, 26, 43, 0.92)",
      color: active ? "#04231f" : "var(--text)",
      border: `2px solid ${color}`,
      borderRadius: 6,
      padding: "5px 10px",
      fontSize: 11,
      fontWeight: active ? 700 : 400,
      cursor: "pointer",
      backdropFilter: "blur(6px)",
    }) as const;

  return (
    <div className="forecast-picker">
      {items.map(({ id, label }) => (
        <button
          key={id}
          type="button"
          style={btn(activeModelId === id, colorForModel(id))}
          onClick={() => onActiveModel(id)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
