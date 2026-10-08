"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { floorLegIndex } from "@/lib/mapOverlays";
import type { ModelRoute, RouteLeg } from "@/lib/types";

const COLS_STORAGE_KEY = "sailing-router-leg-cols-v1";

type ColId = "time" | "tws" | "twd" | "twa" | "hdg" | "sog" | "cog" | "man";

const COLUMNS: { id: ColId; label: string; required?: boolean }[] = [
  { id: "time", label: "Time", required: true },
  { id: "tws", label: "TWS" },
  { id: "twd", label: "TWD" },
  { id: "twa", label: "TWA" },
  { id: "hdg", label: "Hdg" },
  { id: "sog", label: "SOG" },
  { id: "cog", label: "COG" },
  { id: "man", label: "Man." },
];

const DEFAULT_VISIBLE: ColId[] = COLUMNS.map((c) => c.id);

function loadVisibleCols(): ColId[] {
  if (typeof window === "undefined") return DEFAULT_VISIBLE;
  try {
    const raw = localStorage.getItem(COLS_STORAGE_KEY);
    if (!raw) return DEFAULT_VISIBLE;
    const parsed = JSON.parse(raw) as ColId[];
    if (!Array.isArray(parsed) || parsed.length === 0) return DEFAULT_VISIBLE;
    const allowed = new Set(COLUMNS.map((c) => c.id));
    const next = parsed.filter((id) => allowed.has(id));
    if (!next.includes("time")) next.unshift("time");
    return next.length > 0 ? next : DEFAULT_VISIBLE;
  } catch {
    return DEFAULT_VISIBLE;
  }
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function cellValue(leg: RouteLeg, id: ColId): string {
  switch (id) {
    case "time":
      return fmtTime(leg.t_utc);
    case "tws":
      return Number(leg.tws).toFixed(1);
    case "twd":
      return String(Math.round(leg.twd));
    case "twa":
      return String(Math.round(leg.twa));
    case "hdg":
      return String(Math.round(leg.heading));
    case "sog":
      return Number(leg.sog).toFixed(1);
    case "cog":
      return String(Math.round(leg.cog));
    case "man":
      return leg.maneuver === "none" ? "—" : leg.maneuver;
  }
}

function FilterIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M4 6h16M7 12h10M10 18h4"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** Leg table pinned so the current time is always the first row. */
export default function RouteLegTable({
  route,
  currentTimeUtc,
  onSeek,
  title = "Route detail",
}: {
  route: ModelRoute | null | undefined;
  currentTimeUtc: string;
  onSeek?: (tUtc: string) => void;
  title?: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [visible, setVisible] = useState<ColId[]>(DEFAULT_VISIBLE);

  useEffect(() => {
    setVisible(loadVisibleCols());
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [menuOpen]);

  const visibleSet = useMemo(() => new Set(visible), [visible]);
  const shownCols = useMemo(
    () => COLUMNS.filter((c) => visibleSet.has(c.id)),
    [visibleSet],
  );

  const toggleCol = (id: ColId) => {
    const meta = COLUMNS.find((c) => c.id === id);
    if (meta?.required) return;
    setVisible((prev) => {
      const has = prev.includes(id);
      const next = has ? prev.filter((x) => x !== id) : [...prev, id];
      if (!next.includes("time")) next.unshift("time");
      try {
        localStorage.setItem(COLS_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const activeIndex = useMemo(
    () => (route?.legs ? floorLegIndex(route.legs, currentTimeUtc) : -1),
    [route, currentTimeUtc],
  );

  /** From the focused leg through the rest of the route — no time-window cut. */
  const forwardLegs = useMemo(() => {
    if (!route?.legs?.length || activeIndex < 0) return [] as { leg: RouteLeg; index: number }[];
    return route.legs.slice(activeIndex).map((leg, offset) => ({
      leg,
      index: activeIndex + offset,
    }));
  }, [route, activeIndex]);

  useEffect(() => {
    if (wrapRef.current) wrapRef.current.scrollTop = 0;
  }, [activeIndex]);

  return (
    <>
      <div className="rail-section-head rail-leg-head">
        <h2>{title}</h2>
        <div className="rail-col-filter" ref={menuRef}>
          <button
            type="button"
            className={`rail-col-filter-btn${menuOpen ? " open" : ""}`}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label="Choose table columns"
            title="Columns"
            onClick={() => setMenuOpen((o) => !o)}
          >
            <FilterIcon />
          </button>
          {menuOpen && (
            <div className="rail-col-filter-menu" role="menu">
              {COLUMNS.map((c) => (
                <label
                  key={c.id}
                  className={`rail-col-filter-item${c.required ? " required" : ""}`}
                  role="menuitemcheckbox"
                  aria-checked={visibleSet.has(c.id)}
                >
                  <input
                    type="checkbox"
                    checked={visibleSet.has(c.id)}
                    disabled={c.required}
                    onChange={() => toggleCol(c.id)}
                  />
                  <span>{c.label}</span>
                </label>
              ))}
            </div>
          )}
        </div>
      </div>

      {!route?.legs?.length ? (
        <p className="rail-leg-empty">No legs for this route.</p>
      ) : (
        <div className="rail-leg-table-wrap" ref={wrapRef}>
          <table className="detail-table rail-leg-table">
            <thead>
              <tr>
                {shownCols.map((c) => (
                  <th key={c.id}>{c.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {forwardLegs.map(({ leg, index }, row) => {
                const isActive = row === 0;
                return (
                  <tr
                    key={`${leg.t_utc}-${index}`}
                    className={isActive ? "detail-row-active" : undefined}
                    onClick={() => onSeek?.(leg.t_utc)}
                    style={{ cursor: onSeek ? "pointer" : undefined }}
                  >
                    {shownCols.map((c) => (
                      <td key={c.id}>{cellValue(leg, c.id)}</td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
