"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Course, Mark } from "@/lib/types";
import { isPointSet } from "@/lib/mapOverlays";
import {
  decimalToDm,
  dmToDecimal,
  formatMin,
  type HemEW,
  type HemNS,
} from "@/lib/coords";

export type EditMode = "none" | "start" | "finish" | "mark";

function MarkDisc({
  kind,
  label,
}: {
  kind: "start" | "finish" | "mark";
  label: string;
}) {
  return (
    <span className={`course-mark-disc ${kind}`} aria-hidden>
      {label}
    </span>
  );
}

function reorderPreview(marks: Mark[], from: number, to: number): Mark[] {
  if (from === to || from < 0 || to < 0 || from >= marks.length || to >= marks.length) {
    return marks;
  }
  const next = [...marks];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

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
  const startSet = isPointSet(course.start);
  const finishSet = isPointSet(course.finish);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);
  const dragIndexRef = useRef<number | null>(null);

  const toggleMode = (mode: EditMode) => {
    onEditMode(editMode === mode ? "none" : mode);
  };

  const clearStart = () => {
    onCourseChange({ ...course, start: [0, 0] });
    if (editMode === "start") onEditMode("none");
  };

  const clearFinish = () => {
    onCourseChange({ ...course, finish: [0, 0] });
    if (editMode === "finish") onEditMode("none");
  };

  const removeMark = (index: number) => {
    onCourseChange({
      ...course,
      marks: course.marks.filter((_, i) => i !== index),
    });
  };

  const updateStart = (lat: number, lon: number) => {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    onCourseChange({ ...course, start: [lat, lon] });
  };

  const updateFinish = (lat: number, lon: number) => {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    onCourseChange({ ...course, finish: [lat, lon] });
  };

  const updateMarkPosition = (index: number, lat: number, lon: number) => {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    onCourseChange({
      ...course,
      marks: course.marks.map((mk, i) =>
        i === index ? { ...mk, position: [lat, lon] } : mk,
      ),
    });
  };

  const reorderMarks = (from: number, to: number) => {
    if (from === to || from < 0 || to < 0) return;
    onCourseChange({ ...course, marks: reorderPreview(course.marks, from, to) });
  };

  const previewMarks = useMemo(() => {
    if (dragIndex == null || overIndex == null) return course.marks;
    return reorderPreview(course.marks, dragIndex, overIndex);
  }, [course.marks, dragIndex, overIndex]);

  const previewSourceIndex = useMemo(() => {
    if (dragIndex == null || overIndex == null) {
      return course.marks.map((_, i) => i);
    }
    const idxs = course.marks.map((_, i) => i);
    const [moved] = idxs.splice(dragIndex, 1);
    idxs.splice(overIndex, 0, moved);
    return idxs;
  }, [course.marks, dragIndex, overIndex]);

  const showList = startSet || finishSet || course.marks.length > 0;

  return (
    <div className="course-editor">
      {!courseReady && (
        <div className="course-editor-hint">
          Click <strong>Set start</strong> then <strong>Set finish</strong> on the map to define your course.
        </div>
      )}

      <div className="course-editor-actions">
        <button
          type="button"
          className={`course-editor-btn${editMode === "start" ? " active" : ""}`}
          disabled={startSet}
          onClick={() => toggleMode("start")}
          title={startSet ? "Start already set — remove it to set again" : "Set start on map"}
        >
          <MarkDisc kind="start" label="S" />
          Set start
        </button>
        <button
          type="button"
          className={`course-editor-btn${editMode === "finish" ? " active" : ""}`}
          disabled={finishSet}
          onClick={() => toggleMode("finish")}
          title={finishSet ? "Finish already set — remove it to set again" : "Set finish on map"}
        >
          <MarkDisc kind="finish" label="F" />
          Set finish
        </button>
        <button
          type="button"
          className={`course-editor-btn${editMode === "mark" ? " active" : ""}`}
          onClick={() => toggleMode("mark")}
          title="Add a mark on the map"
        >
          <MarkDisc kind="mark" label="+" />
          Mark
        </button>
      </div>

      {editMode !== "none" && (
        <div className="course-editor-hint">
          Click the map to {editMode === "mark" ? "add a mark" : `set the ${editMode}`}.
        </div>
      )}

      {showList && (
        <div className="course-point-list">
          {startSet && (
            <PointRow
              disc={<MarkDisc kind="start" label="S" />}
              lat={course.start[0]}
              lon={course.start[1]}
              editable
              pinned
              onCoordsChange={updateStart}
              onRemove={clearStart}
              removeLabel="Remove start"
            />
          )}

          {previewMarks.map((mk, previewIdx) => {
            const sourceIdx = previewSourceIndex[previewIdx] ?? previewIdx;
            const isDragging = dragIndex === sourceIdx;
            return (
              <PointRow
                key={`mark-${sourceIdx}`}
                disc={<MarkDisc kind="mark" label={String(previewIdx + 1)} />}
                lat={mk.position[0]}
                lon={mk.position[1]}
                editable
                onCoordsChange={(lat, lon) => updateMarkPosition(sourceIdx, lat, lon)}
                draggable
                dragging={isDragging}
                onRemove={() => removeMark(sourceIdx)}
                removeLabel={`Remove mark ${previewIdx + 1}`}
                onDragStart={() => {
                  dragIndexRef.current = sourceIdx;
                  setDragIndex(sourceIdx);
                  setOverIndex(previewIdx);
                }}
                onDragOver={() => setOverIndex(previewIdx)}
                onDrop={() => {
                  const from = dragIndexRef.current;
                  if (from != null) reorderMarks(from, previewIdx);
                  dragIndexRef.current = null;
                  setDragIndex(null);
                  setOverIndex(null);
                }}
                onDragEnd={() => {
                  dragIndexRef.current = null;
                  setDragIndex(null);
                  setOverIndex(null);
                }}
              />
            );
          })}

          {finishSet && (
            <PointRow
              disc={<MarkDisc kind="finish" label="F" />}
              lat={course.finish[0]}
              lon={course.finish[1]}
              editable
              pinned
              onCoordsChange={updateFinish}
              onRemove={clearFinish}
              removeLabel="Remove finish"
            />
          )}
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
      <div className="course-editor-footer">Course auto-saves in this browser.</div>
    </div>
  );
}

function PointRow({
  disc,
  lat,
  lon,
  editable = false,
  pinned = false,
  draggable = false,
  dragging = false,
  onRemove,
  removeLabel,
  onCoordsChange,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: {
  disc: React.ReactNode;
  lat?: number;
  lon?: number;
  editable?: boolean;
  pinned?: boolean;
  draggable?: boolean;
  dragging?: boolean;
  onRemove: () => void;
  removeLabel: string;
  onCoordsChange?: (lat: number, lon: number) => void;
  onDragStart?: () => void;
  onDragOver?: () => void;
  onDrop?: () => void;
  onDragEnd?: () => void;
}) {
  return (
    <div
      className={["course-point-row", dragging ? "dragging" : ""].filter(Boolean).join(" ")}
      draggable={draggable}
      onDragStart={(e) => {
        if (!draggable) return;
        const t = e.target as HTMLElement;
        if (t.tagName === "INPUT" || t.tagName === "SELECT") {
          e.preventDefault();
          return;
        }
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", "mark");
        onDragStart?.();
      }}
      onDragOver={(e) => {
        if (!draggable) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        onDragOver?.();
      }}
      onDrop={(e) => {
        if (!draggable) return;
        e.preventDefault();
        onDrop?.();
      }}
      onDragEnd={() => onDragEnd?.()}
    >
      <span
        className={`course-point-handle${pinned ? " pinned" : ""}`}
        aria-hidden={!draggable}
        title={draggable ? "Drag to reorder" : undefined}
      >
        ⋮⋮
      </span>
      {disc}
      {editable && lat != null && lon != null && onCoordsChange ? (
        <CoordEditors lat={lat} lon={lon} onCoordsChange={onCoordsChange} />
      ) : null}
      <button
        type="button"
        className="course-point-remove"
        onClick={onRemove}
        aria-label={removeLabel}
        title={removeLabel}
      >
        ×
      </button>
    </div>
  );
}

function CoordEditors({
  lat,
  lon,
  onCoordsChange,
}: {
  lat: number;
  lon: number;
  onCoordsChange: (lat: number, lon: number) => void;
}) {
  const latParts = decimalToDm(lat, "lat");
  const lonParts = decimalToDm(lon, "lon");

  const [latDeg, setLatDeg] = useState(String(latParts.deg));
  const [latMin, setLatMin] = useState(formatMin(latParts.min));
  const [latHem, setLatHem] = useState<HemNS>(latParts.hem as HemNS);
  const [lonDeg, setLonDeg] = useState(String(lonParts.deg));
  const [lonMin, setLonMin] = useState(formatMin(lonParts.min));
  const [lonHem, setLonHem] = useState<HemEW>(lonParts.hem as HemEW);

  const focused = useRef(false);

  useEffect(() => {
    if (focused.current) return;
    const la = decimalToDm(lat, "lat");
    const lo = decimalToDm(lon, "lon");
    setLatDeg(String(la.deg));
    setLatMin(formatMin(la.min));
    setLatHem(la.hem as HemNS);
    setLonDeg(String(lo.deg));
    setLonMin(formatMin(lo.min));
    setLonHem(lo.hem as HemEW);
  }, [lat, lon]);

  const commit = (next?: {
    latDeg?: string;
    latMin?: string;
    latHem?: HemNS;
    lonDeg?: string;
    lonMin?: string;
    lonHem?: HemEW;
  }) => {
    focused.current = false;
    const laDeg = next?.latDeg ?? latDeg;
    const laMin = next?.latMin ?? latMin;
    const laHem = next?.latHem ?? latHem;
    const loDeg = next?.lonDeg ?? lonDeg;
    const loMin = next?.lonMin ?? lonMin;
    const loHem = next?.lonHem ?? lonHem;

    const nextLat = dmToDecimal(
      { deg: parseInt(laDeg, 10) || 0, min: parseFloat(laMin) || 0, hem: laHem },
      "lat",
    );
    const nextLon = dmToDecimal(
      { deg: parseInt(loDeg, 10) || 0, min: parseFloat(loMin) || 0, hem: loHem },
      "lon",
    );
    onCoordsChange(nextLat, nextLon);

    const la = decimalToDm(nextLat, "lat");
    const lo = decimalToDm(nextLon, "lon");
    setLatDeg(String(la.deg));
    setLatMin(formatMin(la.min));
    setLatHem(la.hem as HemNS);
    setLonDeg(String(lo.deg));
    setLonMin(formatMin(lo.min));
    setLonHem(lo.hem as HemEW);
  };

  const onEnterBlur = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") (e.target as HTMLElement).blur();
  };

  return (
    <span className="course-point-coords-edit">
      <span className="course-point-dm">
        <input
          type="number"
          min={0}
          max={90}
          step={1}
          className="course-point-coord-input course-point-deg"
          value={latDeg}
          aria-label="Latitude degrees"
          title="Latitude degrees"
          onFocus={() => {
            focused.current = true;
          }}
          onChange={(e) => setLatDeg(e.target.value)}
          onBlur={() => commit()}
          onKeyDown={onEnterBlur}
          onMouseDown={(e) => e.stopPropagation()}
          onDragStart={(e) => e.preventDefault()}
        />
        <span className="course-point-coord-unit">°</span>
        <input
          type="number"
          min={0}
          max={59.999}
          step={0.001}
          className="course-point-coord-input course-point-min"
          value={latMin}
          aria-label="Latitude minutes"
          title="Latitude minutes"
          onFocus={() => {
            focused.current = true;
          }}
          onChange={(e) => setLatMin(e.target.value)}
          onBlur={() => commit()}
          onKeyDown={onEnterBlur}
          onMouseDown={(e) => e.stopPropagation()}
          onDragStart={(e) => e.preventDefault()}
        />
        <span className="course-point-coord-unit">′</span>
        <select
          className="course-point-hem"
          value={latHem}
          aria-label="Latitude hemisphere"
          onFocus={() => {
            focused.current = true;
          }}
          onChange={(e) => {
            const hem = e.target.value as HemNS;
            setLatHem(hem);
            commit({ latHem: hem });
          }}
          onMouseDown={(e) => e.stopPropagation()}
          onDragStart={(e) => e.preventDefault()}
        >
          <option value="N">N</option>
          <option value="S">S</option>
        </select>
      </span>
      <span className="course-point-dm">
        <input
          type="number"
          min={0}
          max={180}
          step={1}
          className="course-point-coord-input course-point-deg-lon"
          value={lonDeg}
          aria-label="Longitude degrees"
          title="Longitude degrees"
          onFocus={() => {
            focused.current = true;
          }}
          onChange={(e) => setLonDeg(e.target.value)}
          onBlur={() => commit()}
          onKeyDown={onEnterBlur}
          onMouseDown={(e) => e.stopPropagation()}
          onDragStart={(e) => e.preventDefault()}
        />
        <span className="course-point-coord-unit">°</span>
        <input
          type="number"
          min={0}
          max={59.999}
          step={0.001}
          className="course-point-coord-input course-point-min"
          value={lonMin}
          aria-label="Longitude minutes"
          title="Longitude minutes"
          onFocus={() => {
            focused.current = true;
          }}
          onChange={(e) => setLonMin(e.target.value)}
          onBlur={() => commit()}
          onKeyDown={onEnterBlur}
          onMouseDown={(e) => e.stopPropagation()}
          onDragStart={(e) => e.preventDefault()}
        />
        <span className="course-point-coord-unit">′</span>
        <select
          className="course-point-hem"
          value={lonHem}
          aria-label="Longitude hemisphere"
          onFocus={() => {
            focused.current = true;
          }}
          onChange={(e) => {
            const hem = e.target.value as HemEW;
            setLonHem(hem);
            commit({ lonHem: hem });
          }}
          onMouseDown={(e) => e.stopPropagation()}
          onDragStart={(e) => e.preventDefault()}
        >
          <option value="E">E</option>
          <option value="W">W</option>
        </select>
      </span>
    </span>
  );
}
