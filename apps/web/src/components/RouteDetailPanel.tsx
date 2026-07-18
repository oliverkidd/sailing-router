"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { colorForModel } from "@/lib/colors";
import type { ModelRoute, RouteLeg } from "@/lib/types";

const WINDOW_BEFORE_MS = 30 * 60 * 1000;
const WINDOW_AFTER_MS = 90 * 60 * 1000;
const MIN_WIDTH = 320;
const MAX_WIDTH = 720;
const DEFAULT_WIDTH = 420;

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function nearestLegIndex(legs: RouteLeg[], tUtc: string): number {
  if (!legs.length) return -1;
  const tMs = new Date(tUtc).getTime();
  if (Number.isNaN(tMs)) return 0;
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < legs.length; i++) {
    const dist = Math.abs(new Date(legs[i].t_utc).getTime() - tMs);
    if (dist < bestDist) {
      bestDist = dist;
      best = i;
    }
  }
  return best;
}

export default function RouteDetailPanel({
  routes,
  routeIndex,
  currentTimeUtc,
  open,
  onToggle,
  onRouteIndex,
  onSeek,
  modelLabels = {},
}: {
  routes: ModelRoute[];
  routeIndex: number;
  currentTimeUtc: string;
  open: boolean;
  onToggle: () => void;
  onRouteIndex: (i: number) => void;
  onSeek?: (tUtc: string) => void;
  modelLabels?: Record<string, string>;
}) {
  const route = routes[routeIndex];
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const activeRowRef = useRef<HTMLTableRowElement | null>(null);

  const windowLegs = useMemo(() => {
    if (!route?.legs?.length) return [] as { leg: RouteLeg; index: number }[];
    const cur = new Date(currentTimeUtc).getTime();
    if (Number.isNaN(cur)) return route.legs.map((leg, index) => ({ leg, index }));
    const lo = cur - WINDOW_BEFORE_MS;
    const hi = cur + WINDOW_AFTER_MS;
    return route.legs
      .map((leg, index) => ({ leg, index }))
      .filter(({ leg }) => {
        const t = new Date(leg.t_utc).getTime();
        return t >= lo && t <= hi;
      });
  }, [route, currentTimeUtc]);

  const activeIndex = useMemo(
    () => (route?.legs ? nearestLegIndex(route.legs, currentTimeUtc) : -1),
    [route, currentTimeUtc],
  );

  const collapsedLeg = route?.legs?.[activeIndex >= 0 ? activeIndex : 0];

  useEffect(() => {
    activeRowRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [activeIndex, open]);

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, drag.startWidth + (drag.startX - e.clientX)));
      setWidth(next);
    };
    const onUp = () => {
      dragRef.current = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, []);

  if (!open) {
    return (
      <aside className="detail-panel collapsed" onClick={onToggle} title="Open route detail">
        <div className="detail-tab">
          <span>{collapsedLeg ? fmtTime(collapsedLeg.t_utc) : "—"}</span>
          {route && <span className="swatch" style={{ background: colorForModel(route.model_id) }} />}
        </div>
      </aside>
    );
  }

  return (
    <aside className="detail-panel expanded" style={{ width }}>
      <div
        className="detail-resizer"
        onPointerDown={(e) => {
          e.preventDefault();
          dragRef.current = { startX: e.clientX, startWidth: width };
        }}
        title="Drag to resize"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize route detail panel"
      />
      <div className="detail-header">
        <h2>Route detail</h2>
        <button type="button" className="detail-close" onClick={onToggle} aria-label="Collapse panel">
          ›
        </button>
      </div>

      {routes.length > 1 && (
        <div className="detail-tabs">
          {routes.map((r, i) => (
            <button
              key={r.model_id}
              type="button"
              className={i === routeIndex ? "active" : ""}
              onClick={() => onRouteIndex(i)}
            >
              <span className="swatch" style={{ background: colorForModel(r.model_id) }} />
              {modelLabels[r.model_id] ?? r.model_id}
            </button>
          ))}
        </div>
      )}

      {!route?.legs?.length ? (
        <p style={{ color: "var(--muted)", fontSize: 12 }}>Run a route to inspect legs.</p>
      ) : (
        <div className="detail-table-wrap">
          <table className="detail-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>TWS</th>
                <th>TWD</th>
                <th>TWA</th>
                <th>Hdg</th>
                <th>SOG</th>
                <th>COG</th>
                <th>Man.</th>
              </tr>
            </thead>
            <tbody>
              {windowLegs.map(({ leg, index }) => {
                const isActive = index === activeIndex;
                return (
                  <tr
                    key={`${leg.t_utc}-${index}`}
                    ref={isActive ? activeRowRef : undefined}
                    className={isActive ? "detail-row-active" : undefined}
                    onClick={() => onSeek?.(leg.t_utc)}
                    style={{ cursor: onSeek ? "pointer" : undefined }}
                  >
                    <td>{fmtTime(leg.t_utc)}</td>
                    <td>{Number(leg.tws).toFixed(1)}</td>
                    <td>{Math.round(leg.twd)}</td>
                    <td>{Math.round(leg.twa)}</td>
                    <td>{Math.round(leg.heading)}</td>
                    <td>{Number(leg.sog).toFixed(1)}</td>
                    <td>{Math.round(leg.cog)}</td>
                    <td>{leg.maneuver === "none" ? "—" : leg.maneuver}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </aside>
  );
}
