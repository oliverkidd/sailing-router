"use client";

import type { Course } from "@/lib/types";

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
  return (
    <div style={{ fontSize: 12 }}>
      {!courseReady && (
        <div style={{ color: "var(--accent)", marginBottom: 8, fontSize: 11 }}>
          Click <strong>Set start</strong> then <strong>Set finish</strong> on the map to define your course.
        </div>
      )}

      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
        <button
          type="button"
          style={btn(editMode === "start")}
          onClick={() => onEditMode(editMode === "start" ? "none" : "start")}
        >
          Set start
        </button>
        <button
          type="button"
          style={btn(editMode === "finish")}
          onClick={() => onEditMode(editMode === "finish" ? "none" : "finish")}
        >
          Set finish
        </button>
        <button
          type="button"
          style={btn(editMode === "mark")}
          onClick={() => onEditMode(editMode === "mark" ? "none" : "mark")}
        >
          + Mark
        </button>
        <button
          type="button"
          aria-label="Clear marks"
          title="Clear marks"
          disabled={course.marks.length === 0}
          onClick={() => onCourseChange({ ...course, marks: [] })}
          style={{
            ...btn(false),
            padding: "4px 8px",
            opacity: course.marks.length === 0 ? 0.4 : 1,
            cursor: course.marks.length === 0 ? "default" : "pointer",
            marginLeft: "auto",
            fontSize: 14,
            lineHeight: 1,
            fontWeight: 600,
          }}
        >
          ×
        </button>
      </div>
      {editMode !== "none" && (
        <div style={{ color: "var(--accent)", marginBottom: 6 }}>
          Click the map to {editMode === "mark" ? "add a mark" : `set the ${editMode}`}.
        </div>
      )}

      <label
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "3px 0",
          color: "var(--muted)",
        }}
      >
        Depart (UTC)
        <input
          type="datetime-local"
          value={course.depart_utc.slice(0, 16)}
          onChange={(e) =>
            onCourseChange({ ...course, depart_utc: new Date(e.target.value + "Z").toISOString() })
          }
          style={{
            background: "#0b1220",
            color: "var(--text)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "3px 5px",
          }}
        />
      </label>
      <div style={{ color: "var(--muted)", fontSize: 10, marginTop: 6 }}>Course auto-saves in this browser.</div>
    </div>
  );
}
