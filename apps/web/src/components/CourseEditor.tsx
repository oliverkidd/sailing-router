"use client";

import type { Course } from "@/lib/types";
import { courseIsReady } from "@/lib/mapOverlays";

export type EditMode = "none" | "start" | "finish" | "mark";

const btn = (active: boolean): React.CSSProperties => ({
  background: active ? "var(--accent)" : "var(--panel-2)",
  color: active ? "#04231f" : "var(--text)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  padding: "4px 8px",
  cursor: "pointer",
  fontSize: 11,
});

export default function CourseEditor({
  course,
  onCourseChange,
  editMode,
  onEditMode,
  courseReady,
}: {
  course: Course;
  onCourseChange: (c: Course) => void;
  editMode: EditMode;
  onEditMode: (m: EditMode) => void;
  courseReady?: boolean;
}) {
  const exportGeoJson = () => {
    const gj = {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: { kind: "start" },
          geometry: { type: "Point", coordinates: [course.start[1], course.start[0]] },
        },
        ...course.marks.map((m, i) => ({
          type: "Feature",
          properties: { kind: "mark", name: m.name ?? `M${i + 1}` },
          geometry: { type: "Point", coordinates: [m.position[1], m.position[0]] },
        })),
        {
          type: "Feature",
          properties: { kind: "finish" },
          geometry: { type: "Point", coordinates: [course.finish[1], course.finish[0]] },
        },
      ],
    };
    const blob = new Blob([JSON.stringify(gj, null, 2)], { type: "application/geo+json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "course.geojson";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div style={{ fontSize: 12 }}>
      {!courseReady && (
        <div style={{ color: "var(--accent)", marginBottom: 8, fontSize: 11 }}>
          Click <strong>Set start</strong> then <strong>Set finish</strong> on the map to define your course.
        </div>
      )}

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
        <button style={btn(editMode === "start")} onClick={() => onEditMode(editMode === "start" ? "none" : "start")}>
          Set start
        </button>
        <button style={btn(editMode === "finish")} onClick={() => onEditMode(editMode === "finish" ? "none" : "finish")}>
          Set finish
        </button>
        <button style={btn(editMode === "mark")} onClick={() => onEditMode(editMode === "mark" ? "none" : "mark")}>
          + Mark
        </button>
        <button style={btn(false)} onClick={() => onCourseChange({ ...course, marks: [] })}>
          Clear marks
        </button>
      </div>
      {editMode !== "none" && (
        <div style={{ color: "var(--accent)", marginBottom: 6 }}>
          Click the map to {editMode === "mark" ? "add a mark" : `set the ${editMode}`}.
        </div>
      )}

      <div style={{ color: "var(--muted)", marginBottom: 8 }}>
        {course.marks.length} mark{course.marks.length === 1 ? "" : "s"}
        {courseIsReady(course) ? (
          <>
            {" "}
            · start {course.start.map((n) => n.toFixed(2)).join(", ")} · finish{" "}
            {course.finish.map((n) => n.toFixed(2)).join(", ")}
          </>
        ) : (
          " · course not set"
        )}
      </div>

      <label style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "3px 0", color: "var(--muted)" }}>
        Depart (UTC)
        <input
          type="datetime-local"
          value={course.depart_utc.slice(0, 16)}
          onChange={(e) => onCourseChange({ ...course, depart_utc: new Date(e.target.value + "Z").toISOString() })}
          style={{ background: "#0b1220", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 6, padding: "3px 5px" }}
        />
      </label>
      <label style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "3px 0", color: "var(--muted)" }}>
        Safety depth (m)
        <input
          type="number"
          step={0.5}
          value={course.min_depth_m}
          onChange={(e) => onCourseChange({ ...course, min_depth_m: Number(e.target.value) })}
          style={{ width: 70, background: "#0b1220", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 6, padding: "3px 5px" }}
        />
      </label>

      <div style={{ marginTop: 8 }}>
        <button style={{ ...btn(false), width: "100%" }} onClick={exportGeoJson} disabled={!courseReady}>
          Export GeoJSON
        </button>
      </div>
      <div style={{ color: "var(--muted)", fontSize: 10, marginTop: 6 }}>Course auto-saves in this browser.</div>
    </div>
  );
}
