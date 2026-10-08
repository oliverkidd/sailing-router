"use client";

import type { Course, ModelPreset } from "@/lib/types";
import {
  courseDistanceNm,
  courseIsReady,
  courseWithEffectiveFinish,
  isPointSet,
} from "@/lib/mapOverlays";

function fmtDepart(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const day = d.getUTCDate();
  const mon = months[d.getUTCMonth()];
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${day} ${mon} ${hh}:${mm} UTC`;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="route-review-row">
      <span className="route-review-label">{label}</span>
      <span className="route-review-value">{value}</span>
    </div>
  );
}

export default function RouteReview({
  course,
  selected,
  running,
  prepState,
  message,
  onRun,
  runDisabledReason,
}: {
  course: Course;
  selected: Set<string>;
  models: ModelPreset[];
  running: boolean;
  prepState: "idle" | "preparing" | "ready" | "error" | "warning";
  message?: string;
  onRun: () => void;
  runDisabledReason?: string;
}) {
  const ready = courseIsReady(course);
  const canRun = !running && selected.size > 0 && ready && !runDisabledReason;
  const resolved = courseWithEffectiveFinish(course);
  const crowNm = ready ? courseDistanceNm(resolved) : 0;
  const markCount = course.marks.filter((m) => isPointSet(m.position)).length;
  const forecastCount = selected.size;

  return (
    <div className="route-review">
      {!ready ? (
        <div className="route-review-empty">
          Add a start and at least one more point to review the course.
        </div>
      ) : (
        <>
          <Row
            label="Marks"
            value={
              markCount === 0 ? "None" : `${markCount} mark${markCount === 1 ? "" : "s"}`
            }
          />
          <Row label="Crow flies" value={`${crowNm.toFixed(1)} nm`} />
          <Row label="Depart" value={fmtDepart(course.depart_utc)} />
          <Row
            label="Forecasts"
            value={
              forecastCount === 0
                ? "None"
                : `${forecastCount} model${forecastCount === 1 ? "" : "s"}`
            }
          />
        </>
      )}

      {prepState === "error" && message && !running && (
        <div className="route-review-error">{message}</div>
      )}

      <button
        type="button"
        onClick={() => onRun()}
        disabled={!canRun}
        className="route-review-run"
        title={runDisabledReason}
        style={{
          background: canRun ? "var(--accent)" : "var(--panel-2)",
          color: canRun ? "#04231f" : "var(--muted)",
          cursor: canRun ? "pointer" : "default",
        }}
      >
        {running
          ? prepState === "preparing"
            ? "Preparing…"
            : "Routing…"
          : `Run ${selected.size || ""} model${selected.size === 1 ? "" : "s"}`}
      </button>
      {runDisabledReason && !running && (
        <div className="route-review-hint">{runDisabledReason}</div>
      )}
    </div>
  );
}
