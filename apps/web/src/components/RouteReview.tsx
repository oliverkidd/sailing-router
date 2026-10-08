"use client";

import type { Course, ModelPreset } from "@/lib/types";
import { courseDistanceNm, courseIsReady } from "@/lib/mapOverlays";

function fmtCoord(lat: number, lon: number): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(2)}°${ns} ${Math.abs(lon).toFixed(2)}°${ew}`;
}

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

export function formatSummaryClipboard(
  course: Course,
  selected: Set<string>,
  models: ModelPreset[],
): string {
  const ready = courseIsReady(course);
  if (!ready) return "Course not ready — set start and finish.";
  const forecastLabels =
    selected.size === 0
      ? "None selected"
      : Array.from(selected)
          .map((id) => models.find((m) => m.id === id)?.label ?? id)
          .join(", ");
  const marks =
    course.marks.length === 0
      ? "None"
      : `${course.marks.length} mark${course.marks.length === 1 ? "" : "s"}`;
  const crow = courseDistanceNm(course);
  return [
    `Start: ${fmtCoord(course.start[0], course.start[1])}`,
    `Finish: ${fmtCoord(course.finish[0], course.finish[1])}`,
    `Marks: ${marks}`,
    `Crow flies: ${crow.toFixed(1)} nm`,
    `Depart: ${fmtDepart(course.depart_utc)}`,
    `Forecasts: ${forecastLabels}`,
  ].join("\n");
}

export default function RouteReview({
  course,
  selected,
  models,
  running,
  prepState,
  message,
  onRun,
}: {
  course: Course;
  selected: Set<string>;
  models: ModelPreset[];
  running: boolean;
  prepState: "idle" | "preparing" | "ready" | "error" | "warning";
  message?: string;
  onRun: () => void;
}) {
  const ready = courseIsReady(course);
  const canRun = !running && selected.size > 0 && ready;
  const forecastLabels =
    selected.size === 0
      ? "None selected"
      : Array.from(selected)
          .map((id) => models.find((m) => m.id === id)?.label ?? id)
          .join(", ");
  const crowNm = ready ? courseDistanceNm(course) : 0;

  return (
    <div className="route-review">
      {!ready ? (
        <div className="route-review-empty">Set start and finish on the map to review the course.</div>
      ) : (
        <>
          <Row label="Start" value={fmtCoord(course.start[0], course.start[1])} />
          <Row label="Finish" value={fmtCoord(course.finish[0], course.finish[1])} />
          <Row
            label="Marks"
            value={
              course.marks.length === 0
                ? "None"
                : `${course.marks.length} mark${course.marks.length === 1 ? "" : "s"}`
            }
          />
          <Row label="Crow flies" value={`${crowNm.toFixed(1)} nm`} />
          <Row label="Depart" value={fmtDepart(course.depart_utc)} />
          <Row label="Forecasts" value={forecastLabels} />
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
    </div>
  );
}
