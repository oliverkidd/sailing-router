"use client";

import { useEffect, useRef, useState } from "react";
import { api, type CourseSummary } from "@/lib/api";
import { courseFromGeoJson, courseToGeoJson } from "@/lib/courseGeoJson";
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
}: {
  course: Course;
  onCourseChange: (c: Course) => void;
  editMode: EditMode;
  onEditMode: (m: EditMode) => void;
}) {
  const [samples, setSamples] = useState<CourseSummary[]>([]);
  const fileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    api.courses().then(setSamples).catch(() => setSamples([]));
  }, []);

  const loadSample = async (id: string) => {
    if (!id) return;
    const c = await api.course(id);
    onCourseChange({
      start: c.start,
      finish: c.finish,
      marks: c.marks ?? [],
      exclusions: c.exclusions ?? [],
      depart_utc: c.depart_utc,
      min_depth_m: c.min_depth_m,
    });
  };

  const exportGeoJson = () => {
    const blob = new Blob([JSON.stringify(courseToGeoJson(course), null, 2)], {
      type: "application/geo+json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "course.geojson";
    a.click();
    URL.revokeObjectURL(url);
  };

  const importGeoJson = async (file: File) => {
    const gj = JSON.parse(await file.text());
    onCourseChange(courseFromGeoJson(gj, course));
  };

  return (
    <div style={{ fontSize: 12 }}>
      <select
        onChange={(e) => loadSample(e.target.value)}
        defaultValue=""
        style={{
          width: "100%",
          background: "#0b1220",
          color: "var(--text)",
          border: "1px solid var(--border)",
          borderRadius: 6,
          padding: "5px 6px",
          marginBottom: 8,
        }}
      >
        <option value="">Load sample course…</option>
        {samples.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
      </select>

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
        {course.marks.length} mark{course.marks.length === 1 ? "" : "s"} · start{" "}
        {course.start.map((n) => n.toFixed(2)).join(", ")} · finish{" "}
        {course.finish.map((n) => n.toFixed(2)).join(", ")}
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

      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <button style={{ ...btn(false), flex: 1 }} onClick={() => fileRef.current?.click()}>
          Import GeoJSON
        </button>
        <button style={{ ...btn(false), flex: 1 }} onClick={exportGeoJson}>
          Export GeoJSON
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".json,.geojson"
          style={{ display: "none" }}
          onChange={(e) => e.target.files?.[0] && importGeoJson(e.target.files[0])}
        />
      </div>
    </div>
  );
}
