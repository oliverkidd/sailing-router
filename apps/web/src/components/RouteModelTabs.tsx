"use client";

import { colorForModel } from "@/lib/colors";
import type { ModelRoute } from "@/lib/types";

/** Forecast model tabs above the playback scrubber (route review only). */
export default function RouteModelTabs({
  routes,
  activeModelId,
  modelLabels = {},
  onActiveModel,
}: {
  routes: ModelRoute[];
  activeModelId: string;
  modelLabels?: Record<string, string>;
  onActiveModel: (id: string) => void;
}) {
  if (routes.length <= 1) return null;

  return (
    <div className="route-model-tabs" role="tablist" aria-label="Forecast model">
      {routes.map((r) => {
        const active = r.model_id === activeModelId;
        const color = colorForModel(r.model_id);
        const label = modelLabels[r.model_id] ?? r.model_id;
        return (
          <button
            key={r.model_id}
            type="button"
            role="tab"
            aria-selected={active}
            className={`route-model-tab${active ? " active" : ""}`}
            style={{
              borderColor: color,
              background: active ? color : "rgba(17, 26, 43, 0.92)",
              color: active ? "#04231f" : "var(--text)",
            }}
            onClick={() => onActiveModel(r.model_id)}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
