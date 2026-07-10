"use client";

import { useState } from "react";
import { colorForModel } from "@/lib/colors";
import type { ModelRoute } from "@/lib/types";

function fmtTime(iso: string): string {
  return new Date(iso).toISOString().slice(5, 16).replace("T", " ");
}

export default function RouteTable({ routes }: { routes: ModelRoute[] }) {
  const [selected, setSelected] = useState(0);
  if (routes.length === 0) return null;
  const route = routes[Math.min(selected, routes.length - 1)];

  return (
    <div style={{ marginTop: 8 }}>
      {routes.length > 1 && (
        <div style={{ display: "flex", gap: 6, marginBottom: 6, flexWrap: "wrap" }}>
          {routes.map((r, i) => (
            <button
              key={r.model_id}
              onClick={() => setSelected(i)}
              style={{
                background: i === selected ? "var(--panel-2)" : "transparent",
                border: `1px solid ${i === selected ? colorForModel(r.model_id) : "var(--border)"}`,
                color: "var(--text)",
                borderRadius: 6,
                padding: "2px 8px",
                cursor: "pointer",
                fontSize: 11,
              }}
            >
              <span className="swatch" style={{ background: colorForModel(r.model_id) }} />
              {r.model_id}
            </button>
          ))}
        </div>
      )}
      <div style={{ maxHeight: 200, overflowY: "auto", fontVariantNumeric: "tabular-nums" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
          <thead>
            <tr style={{ color: "var(--muted)", textAlign: "right" }}>
              <th style={{ textAlign: "left" }}>Time</th>
              <th>TWS</th>
              <th>TWD</th>
              <th>TWA</th>
              <th>Hdg</th>
              <th>BSP</th>
              <th>SOG</th>
              <th>Set/Rate</th>
              <th>Mnvr</th>
            </tr>
          </thead>
          <tbody>
            {route.legs
              .filter((_, i) => i % Math.ceil(route.legs.length / 40 || 1) === 0)
              .map((leg, i) => (
                <tr key={i} style={{ textAlign: "right", borderTop: "1px solid var(--border)" }}>
                  <td style={{ textAlign: "left" }}>{fmtTime(leg.t_utc)}</td>
                  <td>{leg.tws.toFixed(1)}</td>
                  <td>{leg.twd.toFixed(0)}</td>
                  <td>{leg.twa.toFixed(0)}</td>
                  <td>{leg.heading.toFixed(0)}</td>
                  <td>{leg.bsp.toFixed(1)}</td>
                  <td>{leg.sog.toFixed(1)}</td>
                  <td>
                    {leg.cur_set_deg.toFixed(0)}/{leg.cur_rate_kn.toFixed(1)}
                  </td>
                  <td style={{ color: leg.maneuver === "none" ? "var(--muted)" : "var(--accent)" }}>
                    {leg.maneuver === "none" ? "" : leg.maneuver}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
