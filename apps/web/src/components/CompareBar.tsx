"use client";

import { colorForModel } from "@/lib/colors";
import type { ModelRoute } from "@/lib/types";

function fmtElapsed(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return `${h}h ${m.toString().padStart(2, "0")}m`;
}

function fmtEta(iso: string): string {
  return new Date(iso).toISOString().slice(5, 16).replace("T", " ") + "Z";
}

export default function CompareBar({ routes }: { routes: ModelRoute[] }) {
  if (routes.length === 0) {
    return <span>Select models and run to compare routes (PredictWind-style overlay).</span>;
  }
  // Fastest first for quick scanning.
  const sorted = [...routes].sort((a, b) => a.elapsed_s - b.elapsed_s);
  return (
    <div style={{ display: "flex", gap: 18, overflowX: "auto" }}>
      {sorted.map((r) => (
        <div key={r.model_id} style={{ whiteSpace: "nowrap", color: "var(--text)" }}>
          <span className="swatch" style={{ background: colorForModel(r.model_id) }} />
          <strong>{r.model_id}</strong>
          <span style={{ color: "var(--muted)" }}>
            {" "}
            · ETA {fmtEta(r.eta_utc)} · {fmtElapsed(r.elapsed_s)} · {r.distance_nm} nm ·{" "}
            {r.n_tacks}T/{r.n_gybes}G{r.forecast_exhausted ? " · ⚠ forecast exhausted" : ""}
          </span>
        </div>
      ))}
    </div>
  );
}
