"use client";

import { useEffect, useRef, useState } from "react";
import type { Course, Mark } from "@/lib/types";
import { isPointSet, withSequentialMarkNames } from "@/lib/mapOverlays";
import {
  decimalToDm,
  dmToDecimal,
  formatMin,
  type HemEW,
  type HemNS,
} from "@/lib/coords";

export type EditMode = "none" | "start" | "finish" | "mark";
export type StartSource = "point" | "location";

const DEFAULT_MARK: Omit<Mark, "position"> = {
  radius_nm: 0.05,
  rounding: "port",
  name: null,
};

/** Collapsed accordion summary for Course. */
export function courseCollapsedSummary(course: Course): string {
  const startOk = isPointSet(course.start);
  if (!startOk) return "No points yet";
  const finishOk = isPointSet(course.finish);
  const n = course.marks.filter((m) => isPointSet(m.position)).length;
  const parts = ["Start"];
  if (n > 0) parts.push(`${n} mark${n === 1 ? "" : "s"}`);
  if (finishOk) parts.push("Finish");
  else if (n > 0) parts.push("Finish = last mark");
  return parts.join(" · ");
}

function TypeSelect({
  kind,
  label,
  open,
  onToggle,
  options,
  onPick,
  locked,
}: {
  kind: "start" | "mark" | "finish";
  label: string;
  open: boolean;
  onToggle: () => void;
  options: { value: "mark" | "finish"; label: string }[];
  onPick: (k: "mark" | "finish") => void;
  locked?: boolean;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) onToggle();
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open, onToggle]);

  return (
    <div className="course-type-wrap" ref={menuRef}>
      <button
        type="button"
        className={`course-type ${kind}${open ? " open" : ""}${locked ? " locked" : ""}`}
        aria-haspopup={locked ? undefined : "listbox"}
        aria-expanded={locked ? undefined : open}
        title={locked ? label : "Change type"}
        disabled={locked}
        onClick={() => {
          if (!locked) onToggle();
        }}
      >
        <span className="course-type-label">{label}</span>
        {!locked && <span className="course-type-caret" aria-hidden />}
      </button>
      {open && !locked && (
        <div className="course-type-menu" role="listbox">
          {options.map((o) => (
            <button
              key={o.value}
              type="button"
              role="option"
              aria-selected={o.value === kind}
              className={o.value === kind ? "active" : ""}
              onClick={() => {
                onPick(o.value);
                onToggle();
              }}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function CourseEditor({
  course,
  onCourseChange,
  editMode,
  onEditMode,
  startSource = "point",
  onStartSource,
  gpsAvailable = false,
  onUseGpsStart,
}: {
  course: Course;
  onCourseChange: (c: Course) => void;
  editMode: EditMode;
  onEditMode: (m: EditMode) => void;
  courseReady?: boolean;
  startSource?: StartSource;
  onStartSource?: (source: StartSource) => void;
  gpsAvailable?: boolean;
  onUseGpsStart?: () => void;
}) {
  const startSet = isPointSet(course.start);
  const finishSet = isPointSet(course.finish);
  const [startMenu, setStartMenu] = useState(false);
  const [openType, setOpenType] = useState<string | null>(null);

  const pendingMarkIdx = course.marks.findIndex((m) => !isPointSet(m.position));
  const hasPendingMark = pendingMarkIdx >= 0;
  const startPending = editMode === "start" && !startSet && startSource === "point";
  const gpsWaiting = startSource === "location" && !startSet;
  const showStartRow = startSet || startPending || gpsWaiting;

  const clearStart = () => {
    onCourseChange({ ...course, start: [0, 0] });
    onStartSource?.("point");
    setStartMenu(false);
    if (editMode === "start") onEditMode("none");
  };

  const clearFinish = () => {
    onCourseChange({ ...course, finish: [0, 0] });
    if (editMode === "finish") onEditMode("none");
  };

  const removeMark = (index: number) => {
    onCourseChange({
      ...course,
      marks: withSequentialMarkNames(course.marks.filter((_, i) => i !== index)),
    });
    if (editMode === "mark" && pendingMarkIdx === index) onEditMode("none");
    setOpenType(null);
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

  const promoteMarkToFinish = (index: number) => {
    const mk = course.marks[index];
    if (!mk || !isPointSet(mk.position)) return;
    const rest = course.marks.filter((_, i) => i !== index);
    const nextMarks = finishSet
      ? withSequentialMarkNames([
          ...rest,
          { ...DEFAULT_MARK, position: course.finish },
        ])
      : withSequentialMarkNames(rest);
    onCourseChange({
      ...course,
      finish: mk.position,
      marks: nextMarks,
    });
    onEditMode("none");
    setOpenType(null);
  };

  const demoteFinishToMark = () => {
    if (!finishSet) return;
    onCourseChange({
      ...course,
      finish: [0, 0],
      marks: withSequentialMarkNames([
        ...course.marks,
        { ...DEFAULT_MARK, position: course.finish },
      ]),
    });
    setOpenType(null);
  };

  const chooseGpsStart = () => {
    setStartMenu(false);
    onStartSource?.("location");
    onEditMode("none");
    onUseGpsStart?.();
  };

  const chooseMapStart = () => {
    setStartMenu(false);
    onStartSource?.("point");
    onEditMode("start");
  };

  const addMark = () => {
    if (!startSet || hasPendingMark) return;
    onCourseChange({
      ...course,
      marks: withSequentialMarkNames([
        ...course.marks,
        { ...DEFAULT_MARK, position: [0, 0] },
      ]),
    });
    onEditMode("mark");
  };

  /** Display index among marks+finish sequence for labels M1, M2… */
  const markLabel = (index: number) => `M${index + 1}`;

  const departDate = course.depart_utc.slice(0, 10);
  const departTime = course.depart_utc.slice(11, 16);

  const setDepartPart = (date: string, time: string) => {
    if (!date || !time) return;
    onCourseChange({
      ...course,
      depart_utc: new Date(`${date}T${time}:00.000Z`).toISOString(),
    });
  };

  return (
    <div className="course-editor course-editor--flat">
      <div className="course-flat-row course-depart-row">
        <span className="course-depart-label">Dep</span>
        <div className="course-flat-main">
          <div className="course-coord-line">
            <div className="course-coord-group course-depart-date">
              <input
                type="date"
                value={departDate}
                aria-label="Depart date UTC"
                onChange={(e) => setDepartPart(e.target.value, departTime)}
              />
            </div>
            <span className="course-coord-sep" aria-hidden />
            <div className="course-coord-group course-depart-time">
              <input
                type="time"
                value={departTime}
                aria-label="Depart time UTC"
                onChange={(e) => setDepartPart(departDate, e.target.value)}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="course-flat-list">
        {!showStartRow && (
          <div className="course-add-block">
            {!startMenu ? (
              <button
                type="button"
                className="course-add-row"
                onClick={() => setStartMenu(true)}
              >
                <span className="course-add-plus" aria-hidden>
                  +
                </span>
                Add start
              </button>
            ) : (
              <div className="course-start-choice">
                <div className="course-start-choice-label">Start from</div>
                <button type="button" onClick={chooseGpsStart}>
                  GPS current location
                </button>
                <button type="button" onClick={chooseMapStart}>
                  Map point
                </button>
                <button
                  type="button"
                  className="course-start-cancel"
                  onClick={() => setStartMenu(false)}
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        )}

        {showStartRow && (
          <div
            className={`course-flat-row${startPending || gpsWaiting ? " pending" : ""}`}
          >
            <TypeSelect
              kind="start"
              label={startSource === "location" ? "GPS" : "S"}
              locked
              open={false}
              onToggle={() => {}}
              options={[]}
              onPick={() => {}}
            />
            <div className="course-flat-main">
              {startPending ? (
                <span className="course-pending-slot">Click on map…</span>
              ) : gpsWaiting ? (
                <span className="course-pending-slot">
                  {gpsAvailable ? "Acquiring fix…" : "Waiting for GPS…"}
                </span>
              ) : (
                <CoordLine
                  lat={course.start[0]}
                  lon={course.start[1]}
                  onChange={updateStart}
                />
              )}
            </div>
            <button
              type="button"
              className="course-flat-remove"
              aria-label="Remove start"
              onClick={clearStart}
            >
              ×
            </button>
          </div>
        )}

        {course.marks.map((mk, index) => {
          const pending = !isPointSet(mk.position);
          const key = `mark-${index}`;
          return (
            <div
              key={key}
              className={`course-flat-row${pending ? " pending" : ""}`}
            >
              <TypeSelect
                kind="mark"
                label={markLabel(index)}
                open={openType === key}
                onToggle={() =>
                  setOpenType((cur) => (cur === key ? null : key))
                }
                options={[
                  { value: "mark", label: "Mark" },
                  { value: "finish", label: "Finish" },
                ]}
                onPick={(k) => {
                  if (k === "finish") promoteMarkToFinish(index);
                }}
              />
              <div className="course-flat-main">
                {pending ? (
                  <span className="course-pending-slot">Click on map…</span>
                ) : (
                  <CoordLine
                    lat={mk.position[0]}
                    lon={mk.position[1]}
                    onChange={(lat, lon) => updateMarkPosition(index, lat, lon)}
                  />
                )}
              </div>
              <button
                type="button"
                className="course-flat-remove"
                aria-label={`Remove ${markLabel(index)}`}
                onClick={() => removeMark(index)}
              >
                ×
              </button>
            </div>
          );
        })}

        {finishSet && (
          <div className="course-flat-row">
            <TypeSelect
              kind="finish"
              label="F"
              open={openType === "finish"}
              onToggle={() =>
                setOpenType((cur) => (cur === "finish" ? null : "finish"))
              }
              options={[
                { value: "finish", label: "Finish" },
                { value: "mark", label: "Mark" },
              ]}
              onPick={(k) => {
                if (k === "mark") demoteFinishToMark();
              }}
            />
            <div className="course-flat-main">
              <CoordLine
                lat={course.finish[0]}
                lon={course.finish[1]}
                onChange={updateFinish}
              />
            </div>
            <button
              type="button"
              className="course-flat-remove"
              aria-label="Remove finish"
              onClick={clearFinish}
            >
              ×
            </button>
          </div>
        )}

        {startSet && !hasPendingMark && !startPending && (
          <button type="button" className="course-add-row" onClick={addMark}>
            <span className="course-add-plus" aria-hidden>
              +
            </span>
            Add mark
          </button>
        )}
      </div>
    </div>
  );
}

function CoordLine({
  lat,
  lon,
  onChange,
}: {
  lat: number;
  lon: number;
  onChange: (lat: number, lon: number) => void;
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
    onChange(nextLat, nextLon);

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
    <div
      className="course-coord-line"
      onFocus={() => {
        focused.current = true;
      }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) commit();
      }}
    >
      <div className="course-coord-group" role="group" aria-label="Latitude">
        <input
          type="number"
          min={0}
          max={90}
          step={1}
          className="course-coord-deg d"
          value={latDeg}
          aria-label="Latitude degrees"
          onChange={(e) => setLatDeg(e.target.value)}
          onKeyDown={onEnterBlur}
        />
        <input
          type="number"
          min={0}
          max={59.999}
          step={0.001}
          className="course-coord-min"
          value={latMin}
          aria-label="Latitude minutes"
          onChange={(e) => setLatMin(e.target.value)}
          onKeyDown={onEnterBlur}
        />
        <select
          className="course-coord-hem"
          value={latHem}
          aria-label="Latitude hemisphere"
          onChange={(e) => {
            const hem = e.target.value as HemNS;
            setLatHem(hem);
            commit({ latHem: hem });
          }}
        >
          <option value="N">N</option>
          <option value="S">S</option>
        </select>
      </div>
      <span className="course-coord-sep" aria-hidden />
      <div className="course-coord-group" role="group" aria-label="Longitude">
        <input
          type="number"
          min={0}
          max={180}
          step={1}
          className="course-coord-deg e"
          value={lonDeg}
          aria-label="Longitude degrees"
          onChange={(e) => setLonDeg(e.target.value)}
          onKeyDown={onEnterBlur}
        />
        <input
          type="number"
          min={0}
          max={59.999}
          step={0.001}
          className="course-coord-min"
          value={lonMin}
          aria-label="Longitude minutes"
          onChange={(e) => setLonMin(e.target.value)}
          onKeyDown={onEnterBlur}
        />
        <select
          className="course-coord-hem"
          value={lonHem}
          aria-label="Longitude hemisphere"
          onChange={(e) => {
            const hem = e.target.value as HemEW;
            setLonHem(hem);
            commit({ lonHem: hem });
          }}
        >
          <option value="E">E</option>
          <option value="W">W</option>
        </select>
      </div>
    </div>
  );
}
