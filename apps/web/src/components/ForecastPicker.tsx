"use client";

import { colorForModel } from "@/lib/colors";
import type { ModelPreset, ModelRoute } from "@/lib/types";
import type { ModelStatus } from "./RouteBuildProgress";

export default function ForecastPicker({
  routes,
  models,
  selectedModels,
  activeModelId,
  onActiveModel,
  overlayStatus = {},
}: {
  routes: ModelRoute[];
  models: ModelPreset[];
  selectedModels: Set<string>;
  activeModelId: string;
  onActiveModel: (id: string) => void;
  overlayStatus?: Record<string, ModelStatus>;
}) {
  const items =
    routes.length > 0
      ? routes.map((r) => {
          const m = models.find((x) => x.id === r.model_id);
          return { id: r.model_id, label: m?.label ?? r.model_id };
        })
      : Array.from(selectedModels).map((id) => {
          const m = models.find((x) => x.id === id);
          return { id, label: m?.label ?? id };
        });

  if (items.length === 0) return null;

  return (
    <div className="forecast-picker">
      {items.map(({ id, label }) => {
        const active = activeModelId === id;
        const color = colorForModel(id);
        const status = overlayStatus[id];
        const errored = status === "error";
        return (
          <button
            key={id}
            type="button"
            className={`forecast-tab${active ? " active" : ""}${errored ? " error" : ""}`}
            style={{
              background: active ? color : "rgba(17, 26, 43, 0.92)",
              color: active ? "#04231f" : "var(--text)",
              border: `2px solid ${errored ? "#f87171" : color}`,
            }}
            onClick={() => onActiveModel(id)}
          >
            {label}
            {status === "fetching" && <span className="forecast-tab-spin" aria-label="loading" />}
            {errored && <span aria-hidden>!</span>}
          </button>
        );
      })}
    </div>
  );
}
