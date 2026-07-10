"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import { api } from "@/lib/api";
import type { BoatConfig, ModelPreset } from "@/lib/types";

// MapLibre must be client-only (accesses window/WebGL).
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

const INITIAL_BOAT: BoatConfig = {
  polar_id: "",
  tack_penalty_s: 30,
  gybe_penalty_s: 20,
  upwind_twa_deg: null,
  downwind_twa_deg: null,
  leeway_coeff: 0,
  leeway_max_deg: 8,
  current_drift_factor: 1,
  polar_efficiency: 1,
};

export default function Home() {
  const [apiUp, setApiUp] = useState<boolean | null>(null);
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [boat, setBoat] = useState<BoatConfig>(INITIAL_BOAT);

  useEffect(() => {
    api
      .health()
      .then(() => setApiUp(true))
      .catch(() => setApiUp(false));
    api
      .models()
      .then(setModels)
      .catch(() => setModels([]));
  }, []);

  return (
    <div id="app-shell">
      <aside className="rail">
        <h1>Sailing Weather Router</h1>
        <div className="subtitle">UK + Mediterranean · local-first</div>

        <div className="rail-section">
          <h2>Course</h2>
          <div style={{ color: "var(--muted)" }}>
            Course editor — draw start, finish, marks, exclusions. (M8)
          </div>
        </div>

        <div className="rail-section">
          <h2>Boat</h2>
          <BoatConfigPanel config={boat} onConfigChange={setBoat} />
        </div>

        <div className="rail-section">
          <h2>Models</h2>
          {models.length === 0 ? (
            <div style={{ color: "var(--muted)" }}>No models loaded.</div>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {models.map((m) => (
                <li key={m.id} style={{ padding: "3px 0" }}>
                  <span className="swatch" style={{ background: m.color }} />
                  {m.label}
                  <span style={{ color: "var(--muted)" }}> · {m.resolution_deg}°</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>

      <div className="map-area">
        <MapView />
      </div>

      <div className="compare-bar">
        <span>
          <span className={`status-dot ${apiUp ? "ok" : "down"}`} />
          API {apiUp === null ? "…" : apiUp ? "connected" : "offline"}
        </span>
        <span>·</span>
        <span>Select models and draw a course to compare routes.</span>
      </div>
    </div>
  );
}
