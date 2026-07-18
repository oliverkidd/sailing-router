"use client";

import { useEffect, useState } from "react";
import type { Course, RouteProgressEvent, RouteProgressStep } from "@/lib/types";

function formatMs(ms?: number): string {
  if (ms == null || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function LiveElapsed({ active, startedAt }: { active: boolean; startedAt?: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || startedAt == null) return;
    const id = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(id);
  }, [active, startedAt]);
  if (!active || startedAt == null) return null;
  return <span className="route-progress-ms">{formatMs(now - startedAt)}</span>;
}

function StatusIcon({ status }: { status: RouteProgressStep["status"] }) {
  if (status === "done")
    return (
      <span className="route-progress-icon route-progress-icon--done" aria-hidden>
        ✓
      </span>
    );
  if (status === "error")
    return (
      <span className="route-progress-icon route-progress-icon--error" aria-hidden>
        !
      </span>
    );
  if (status === "active") return <span className="map-load-spin map-load-spin--sm" aria-hidden />;
  return <span className="route-progress-icon route-progress-icon--pending" aria-hidden />;
}

function markLabel(course: Course, index: number): string {
  const m = course.marks[index];
  if (m?.name?.trim()) return m.name.trim();
  return `Mark ${index + 1}`;
}

/** Ordered waypoint names for leg rows: Start, Mark 1…, Finish. */
export function courseLegEndpoints(course: Course): string[] {
  const names = ["Start"];
  for (let i = 0; i < course.marks.length; i++) names.push(markLabel(course, i));
  names.push("Finish");
  return names;
}

function StepList({ steps }: { steps: RouteProgressStep[] }) {
  const visible = steps.filter((s) => !s.id.startsWith("wind_extend:"));
  return (
    <ol className="route-progress-list">
      {visible.map((step) => (
        <li
          key={step.id}
          className={`route-progress-row route-progress-row--${step.status}`}
        >
          <StatusIcon status={step.status} />
          <div className="route-progress-row-body">
            <div className="route-progress-label">{step.label}</div>
            {step.detail && (
              <div className="route-progress-detail">{step.detail}</div>
            )}
          </div>
          {step.status === "active" ? (
            <LiveElapsed active startedAt={step.ms} />
          ) : step.ms != null && step.status === "done" ? (
            <span className="route-progress-ms">{formatMs(step.ms)}</span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function isSharedStep(id: string): boolean {
  return id === "land" || id === "depth";
}

function modelIdFromStep(id: string): string | null {
  if (id.startsWith("wind:") || id.startsWith("wind_extend:") || id.startsWith("prepare:")) {
    return id.split(":").slice(1).join(":");
  }
  if (id.startsWith("leg:")) {
    // leg:modelId:index — model id may contain no colons typically
    const parts = id.split(":");
    if (parts.length >= 3) return parts.slice(1, -1).join(":");
  }
  return null;
}

function isForecastExhaustedWarning(message: string): boolean {
  return /forecast exhausted/i.test(message);
}

/** Parallel-aware checklist: shared geo + one card per model. */
export default function RouteProgress({
  steps,
  modelIds,
  modelLabels = {},
  warnings,
  showActions,
  retryableModelIds = [],
  onRetry,
  onRetryModel,
  onEdit,
}: {
  steps: RouteProgressStep[];
  modelIds: string[];
  modelLabels?: Record<string, string>;
  warnings?: string[];
  showActions?: boolean;
  /** Models that can be retried independently (failed / unfinished). */
  retryableModelIds?: string[];
  onRetry?: () => void;
  onRetryModel?: (modelId: string) => void;
  onEdit?: () => void;
}) {
  const hasError = steps.some((s) => s.status === "error") || !!showActions;
  const shared = steps.filter((s) => isSharedStep(s.id));
  const multi = modelIds.length > 1;
  const visibleWarnings = (warnings ?? []).filter((w) => !isForecastExhaustedWarning(w));
  const retrySet = new Set(retryableModelIds);

  return (
    <div className="route-progress">
      {shared.length > 0 && (
        <div className="route-progress-shared">
          <StepList steps={shared} />
        </div>
      )}

      {multi ? (
        <div
          className="route-progress-models"
          style={{ gridTemplateColumns: `repeat(${modelIds.length}, minmax(0, 1fr))` }}
        >
          {modelIds.map((mid) => {
            const modelSteps = steps.filter((s) => modelIdFromStep(s.id) === mid);
            const canRetry =
              !!onRetryModel &&
              (retrySet.has(mid) || modelSteps.some((s) => s.status === "error"));
            return (
              <div key={mid} className="route-progress-model-card">
                <div className="route-progress-model-head">
                  <div className="route-progress-model-title">
                    {modelLabels[mid] ?? mid}
                  </div>
                  {canRetry && (
                    <button
                      type="button"
                      className="route-progress-btn route-progress-btn--card"
                      onClick={() => onRetryModel(mid)}
                    >
                      Retry
                    </button>
                  )}
                </div>
                <StepList steps={modelSteps} />
              </div>
            );
          })}
        </div>
      ) : (
        <>
          <StepList steps={steps.filter((s) => !isSharedStep(s.id))} />
          {modelIds.length === 1 &&
            onRetryModel &&
            (retrySet.has(modelIds[0]) ||
              steps.some(
                (s) => modelIdFromStep(s.id) === modelIds[0] && s.status === "error",
              )) && (
              <div className="route-progress-actions">
                <button
                  type="button"
                  className="route-progress-btn route-progress-btn--primary"
                  onClick={() => onRetryModel(modelIds[0])}
                >
                  Retry
                </button>
                {onEdit && (
                  <button type="button" className="route-progress-btn" onClick={onEdit}>
                    Edit route
                  </button>
                )}
              </div>
            )}
        </>
      )}

      {visibleWarnings.length > 0 && (
        <ul className="route-progress-warnings">
          {visibleWarnings.slice(0, 4).map((w, i) => (
            <li key={i}>{w}</li>
          ))}
          {visibleWarnings.length > 4 && <li>+{visibleWarnings.length - 4} more…</li>}
        </ul>
      )}
      {hasError && multi && onEdit && (
        <div className="route-progress-actions">
          {onRetry && (
            <button
              type="button"
              className="route-progress-btn route-progress-btn--primary"
              onClick={onRetry}
            >
              Retry all
            </button>
          )}
          <button type="button" className="route-progress-btn" onClick={onEdit}>
            Edit route
          </button>
        </div>
      )}
      {hasError && !multi && !onRetryModel && (onRetry || onEdit) && (
        <div className="route-progress-actions">
          {onRetry && (
            <button
              type="button"
              className="route-progress-btn route-progress-btn--primary"
              onClick={onRetry}
            >
              Retry
            </button>
          )}
          {onEdit && (
            <button type="button" className="route-progress-btn" onClick={onEdit}>
              Edit route
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** Mark sibling actives done only within the same model (or shared scope). */
function clearActiveInScope(steps: RouteProgressStep[], scopeModelId: string | null, exceptId: string) {
  for (const s of steps) {
    if (s.id === exceptId || s.status !== "active") continue;
    const mid = modelIdFromStep(s.id);
    if (scopeModelId == null) {
      // Shared stage (land/depth): only clear other shared actives
      if (isSharedStep(s.id)) s.status = "done";
    } else if (mid === scopeModelId) {
      s.status = "done";
    }
  }
}

/** Reduce SSE events into status updates on a pre-built checklist. */
export function applyRouteEvent(
  steps: RouteProgressStep[],
  event: RouteProgressEvent,
): RouteProgressStep[] {
  const next = steps.map((s) => ({ ...s }));

  const patchById = (id: string, patch: Partial<RouteProgressStep>) => {
    const idx = next.findIndex((s) => s.id === id);
    if (idx < 0) return false;
    next[idx] = { ...next[idx], ...patch };
    return true;
  };

  if (event.type === "stage") {
    // Keep the current leg spinning while forecast is extended — silent stage.
    if (event.key === "wind_extend") {
      return next;
    }

    const id =
      event.key === "wind" || event.key === "prepare"
        ? `${event.key}:${event.model_id ?? "model"}`
        : event.key;
    const scope = event.model_id ?? (isSharedStep(id) ? null : modelIdFromStep(id));

    if (event.status === "start") {
      clearActiveInScope(next, scope, id);
      if (!patchById(id, { status: "active", ms: Date.now(), detail: undefined })) {
        if (event.key === "prepare") {
          const windId = `wind:${event.model_id ?? "model"}`;
          const after = next.findIndex((s) => s.id === windId);
          const row: RouteProgressStep = {
            id,
            label: "Preparing route",
            status: "active",
            ms: Date.now(),
          };
          if (after >= 0) next.splice(after + 1, 0, row);
          else next.push(row);
        }
      }
    } else if (event.status === "done") {
      patchById(id, {
        status: "done",
        ms: event.ms,
        detail: event.n_times != null ? `${event.n_times} time steps` : undefined,
      });
    } else if (event.status === "error") {
      patchById(id, { status: "error", detail: "failed" });
    }
  } else if (event.type === "leg") {
    const id = `leg:${event.model_id}:${event.index}`;
    if (event.status === "start") {
      clearActiveInScope(next, event.model_id, id);
      patchById(id, { status: "active", ms: Date.now(), detail: undefined });
    } else if (event.status === "done") {
      patchById(id, {
        status: "done",
        ms: event.ms,
        detail: event.termination
          ? `${event.termination}${event.steps != null ? ` · ${event.steps} steps` : ""}`
          : undefined,
      });
    } else if (event.status === "error") {
      // Forecast exhausted → keep spinning; extend + re-route continue quietly.
      if (event.termination === "forecast_exhausted") {
        patchById(id, { status: "active", ms: Date.now(), detail: undefined });
      } else {
        patchById(id, {
          status: "error",
          ms: event.ms,
          detail: event.termination
            ? `${event.termination}${event.steps != null ? ` · ${event.steps} steps` : ""}`
            : "unreachable",
        });
      }
    }
  } else if (event.type === "error") {
    if (isForecastExhaustedWarning(event.message)) {
      return next;
    }
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

export function initialRouteSteps(modelIds: string[], course: Course): RouteProgressStep[] {
  const endpoints = courseLegEndpoints(course);
  const steps: RouteProgressStep[] = [
    { id: "land", label: "Fetching coastal boundaries", status: "pending" },
    { id: "depth", label: "Fetching depth contours", status: "pending" },
  ];
  for (const id of modelIds) {
    steps.push({
      id: `wind:${id}`,
      label: "Fetching forecast",
      status: "pending",
    });
    steps.push({
      id: `prepare:${id}`,
      label: "Preparing route",
      status: "pending",
    });
    for (let i = 0; i < endpoints.length - 1; i++) {
      steps.push({
        id: `leg:${id}:${i}`,
        label: `${endpoints[i]} → ${endpoints[i + 1]}`,
        status: "pending",
      });
    }
  }
  return steps;
}

/** Reset one model's checklist rows (keeps shared geo + other models). */
export function resetModelSteps(
  steps: RouteProgressStep[],
  modelId: string,
  course: Course,
): RouteProgressStep[] {
  const fresh = initialRouteSteps([modelId], course).filter((s) => !isSharedStep(s.id));
  const shared = steps.filter((s) => isSharedStep(s.id));
  const others = steps.filter((s) => {
    const mid = modelIdFromStep(s.id);
    return mid != null && mid !== modelId;
  });
  return [...shared, ...fresh, ...others];
}
