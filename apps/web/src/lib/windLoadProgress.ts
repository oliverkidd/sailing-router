/** Wind browse load checklist — same RouteProgressStep shape as route SSE. */

import type { RouteProgressStep } from "@/lib/types";

export type WindProgressEvent =
  | {
      type: "stage";
      key: "forcing" | "library" | "regrid" | "payload" | string;
      status: "start" | "done" | "error";
      ms?: number;
      terrain_library?: string;
      cache_hit?: boolean;
    }
  | { type: "error"; message: string }
  | { type: "result"; series: Record<string, unknown> };

const WIND_LABELS: Record<string, string> = {
  forcing: "Fetch forcing",
  library: "Attach terrain library",
  regrid: "Downscale / regrid display grid",
  payload: "Build forecast payload",
};

export function initialWindSteps(terrainOn: boolean): RouteProgressStep[] {
  return [
    { id: "forcing", label: WIND_LABELS.forcing, status: "pending" },
    {
      id: "library",
      label: terrainOn ? WIND_LABELS.library : "Terrain library (off)",
      status: "pending",
    },
    { id: "regrid", label: WIND_LABELS.regrid, status: "pending" },
    { id: "payload", label: WIND_LABELS.payload, status: "pending" },
  ];
}

export function applyWindEvent(
  steps: RouteProgressStep[],
  event: WindProgressEvent,
): RouteProgressStep[] {
  const next = steps.map((s) => ({ ...s }));
  const patchById = (id: string, patch: Partial<RouteProgressStep>) => {
    const idx = next.findIndex((s) => s.id === id);
    if (idx < 0) return;
    next[idx] = { ...next[idx], ...patch };
  };

  if (event.type === "stage") {
    const id = event.key;
    if (event.status === "start") {
      for (const s of next) {
        if (s.status === "active" && s.id !== id) s.status = "done";
      }
      patchById(id, {
        status: "active",
        ms: Date.now(),
        detail: undefined,
        label: WIND_LABELS[id] ?? next.find((s) => s.id === id)?.label ?? id,
      });
    } else if (event.status === "done") {
      let detail: string | undefined;
      if (event.key === "library" && event.terrain_library) {
        detail = event.terrain_library;
      } else if (event.key === "regrid" && event.cache_hit) {
        detail = "cache hit";
      }
      patchById(id, {
        status: "done",
        ms: event.ms,
        detail,
      });
    } else if (event.status === "error") {
      patchById(id, { status: "error", detail: "failed" });
    }
  } else if (event.type === "error") {
    const activeIdx = next.findIndex((s) => s.status === "active");
    if (activeIdx >= 0) {
      next[activeIdx] = {
        ...next[activeIdx],
        status: "error",
        detail: event.message,
      };
    }
  }

  return next;
}
