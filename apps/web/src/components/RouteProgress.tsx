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

/** Step checklist — no title header; error footer with Retry / Edit. */
export default function RouteProgress({
  steps,
  warnings,
  showActions,
  onRetry,
  onEdit,
}: {
  steps: RouteProgressStep[];
  warnings?: string[];
  showActions?: boolean;
  onRetry?: () => void;
  onEdit?: () => void;
}) {
  const hasError = steps.some((s) => s.status === "error") || !!showActions;

  return (
    <div className="route-progress">
      <ol className="route-progress-list">
        {steps.map((step) => (
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
      {warnings && warnings.length > 0 && (
        <ul className="route-progress-warnings">
          {warnings.slice(0, 4).map((w, i) => (
            <li key={i}>{w}</li>
          ))}
          {warnings.length > 4 && <li>+{warnings.length - 4} more…</li>}
        </ul>
      )}
      {hasError && (onRetry || onEdit) && (
        <div className="route-progress-actions">
          {onRetry && (
            <button type="button" className="route-progress-btn route-progress-btn--primary" onClick={onRetry}>
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
    const id =
      event.key === "wind" || event.key === "wind_extend"
        ? `${event.key}:${event.model_id ?? "model"}`
        : event.key;
    if (event.status === "start") {
      for (const s of next) {
        if (s.status === "active" && s.id !== id) s.status = "done";
      }
      if (!patchById(id, { status: "active", ms: Date.now(), detail: undefined })) {
        // wind_extend is dynamic — append after matching wind step if missing
        if (event.key === "wind_extend") {
          const windId = `wind:${event.model_id ?? "model"}`;
          const after = next.findIndex((s) => s.id === windId);
          const row: RouteProgressStep = {
            id,
            label: `Extend wind +${event.extend_h ?? 3}h${event.model_id ? ` (${event.model_id})` : ""}`,
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
      for (const s of next) {
        if (s.status === "active") s.status = "done";
      }
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
      patchById(id, {
        status: "error",
        ms: event.ms,
        detail: event.termination
          ? `${event.termination}${event.steps != null ? ` · ${event.steps} steps` : ""}`
          : "unreachable",
      });
      // Leave later legs pending
      for (const s of next) {
        if (s.id.startsWith(`leg:${event.model_id}:`) && s.status === "active") {
          s.status = "error";
        }
      }
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
  } else if (event.type === "warning") {
    // Warnings alone don't flip steps; unfinished is handled via leg error / result.
  }

  return next;
}

export function initialRouteSteps(modelIds: string[], course: Course): RouteProgressStep[] {
  const endpoints = courseLegEndpoints(course);
  const steps: RouteProgressStep[] = [
    { id: "land", label: "Load land", status: "pending" },
    { id: "depth", label: "Load depth", status: "pending" },
  ];
  for (const id of modelIds) {
    steps.push({
      id: `wind:${id}`,
      label: `Fetch wind (${id})`,
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
