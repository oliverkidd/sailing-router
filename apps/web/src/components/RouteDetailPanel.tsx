"use client";

import { colorForModel } from "@/lib/colors";
import type { ModelRoute, RouteLeg } from "@/lib/types";

export default function RouteDetailPanel({
  routes,
  routeIndex,
  legIndex,
  open,
  onToggle,
  onRouteIndex,
}: {
  routes: ModelRoute[];
  routeIndex: number;
  legIndex: number;
  open: boolean;
  onToggle: () => void;
  onRouteIndex: (i: number) => void;
}) {
  const route = routes[routeIndex];
  const leg: RouteLeg | undefined = route?.legs[legIndex];

  if (!open) {
    return (
      <aside className="detail-panel collapsed" onClick={onToggle} title="Open route detail">
        <div className="detail-tab">
          <span>{leg ? new Date(leg.t_utc).toISOString().slice(11, 16) : "—"}</span>
          {route && <span className="swatch" style={{ background: colorForModel(route.model_id) }} />}
        </div>
      </aside>
    );
  }

  return (
    <aside className="detail-panel expanded">
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
              {r.model_id}
            </button>
          ))}
        </div>
      )}

      {!leg ? (
        <p style={{ color: "var(--muted)", fontSize: 12 }}>Run a route to inspect legs.</p>
      ) : (
        <>
          <dl className="detail-grid">
            <dt>Time (UTC)</dt>
            <dd>{new Date(leg.t_utc).toISOString().replace("T", " ").slice(0, 19)}</dd>
            <dt>Position</dt>
            <dd>
              {leg.position[0].toFixed(4)}, {leg.position[1].toFixed(4)}
            </dd>
            <dt>TWS / TWD</dt>
            <dd>
              {leg.tws} kn · {leg.twd}°
            </dd>
            <dt>TWA / Hdg</dt>
            <dd>
              {leg.twa}° · {leg.heading}°
            </dd>
            <dt>BSP / SOG</dt>
            <dd>
              {leg.bsp} kn · {leg.sog} kn
            </dd>
            <dt>COG</dt>
            <dd>{leg.cog}°</dd>
            <dt>Maneuver</dt>
            <dd>{leg.maneuver}</dd>
          </dl>
        </>
      )}
    </aside>
  );
}
