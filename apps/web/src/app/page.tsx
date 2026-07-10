"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import RouteTable from "@/components/RouteTable";
import { api } from "@/lib/api";
import type { BoatConfig, Course, ModelPreset, ModelRoute } from "@/lib/types";

// MapLibre must be client-only (accesses window/WebGL).
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

// A default Aegean course so the app is usable before the full CourseEditor (M8).
// Depart today 00:00 UTC so it aligns with the live forecast window.
function todayMidnightUtc(): string {
  const d = new Date();
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 0, 0, 0)).toISOString();
}

const DEFAULT_COURSE: Course = {
  start: [36.2, 24.6],
  finish: [37.0, 24.6],
  marks: [],
  exclusions: [],
  depart_utc: todayMidnightUtc(),
  min_depth_m: 3,
};

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
  const [course] = useState<Course>(DEFAULT_COURSE);
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string>("");
  const [useCurrents, setUseCurrents] = useState(false);
  const [useTerrain, setUseTerrain] = useState(false);

  const runRoute = async (modelIds: string[]) => {
    if (!boat.polar_id) {
      setRunMsg("Load a polar first.");
      return;
    }
    setRunning(true);
    setRunMsg("Routing…");
    try {
      const res = await api.route({
        course,
        boat,
        model_ids: modelIds,
        use_terrain_wind: useTerrain,
        use_currents: useCurrents,
        time_step_s: 300,
        heading_step_deg: 7.5,
      });
      setRoutes(res.routes);
      setRunMsg(
        res.warnings.length ? res.warnings.join(" ") : `${res.routes.length} route(s) computed.`,
      );
    } catch (e) {
      setRunMsg(String(e));
    } finally {
      setRunning(false);
    }
  };

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
          <div style={{ display: "flex", gap: 12, margin: "8px 0", fontSize: 12 }}>
            <label style={{ color: "var(--muted)", cursor: "pointer" }}>
              <input type="checkbox" checked={useCurrents} onChange={(e) => setUseCurrents(e.target.checked)} /> Currents
            </label>
            <label style={{ color: "var(--muted)", cursor: "pointer" }}>
              <input type="checkbox" checked={useTerrain} onChange={(e) => setUseTerrain(e.target.checked)} /> Terrain wind
            </label>
          </div>
          <button
            onClick={() => runRoute(["ecmwf"])}
            disabled={running}
            style={{
              width: "100%",
              marginTop: 8,
              background: running ? "var(--panel-2)" : "var(--accent)",
              color: running ? "var(--muted)" : "#04231f",
              border: "none",
              borderRadius: 6,
              padding: "6px 8px",
              fontWeight: 600,
              cursor: running ? "default" : "pointer",
            }}
          >
            {running ? "Routing…" : "Run route (ECMWF)"}
          </button>
          {runMsg && <div style={{ color: "var(--muted)", fontSize: 11, marginTop: 6 }}>{runMsg}</div>}
        </div>

        <div className="rail-section">
          <h2>Route detail</h2>
          <RouteTable routes={routes} />
          {routes.length === 0 && (
            <div style={{ color: "var(--muted)", fontSize: 12 }}>Run a route to see legs.</div>
          )}
        </div>
      </aside>

      <div className="map-area">
        <MapView routes={routes} course={course} showCurrents={useCurrents} />
      </div>

      <div className="compare-bar">
        <span>
          <span className={`status-dot ${apiUp ? "ok" : "down"}`} />
          API {apiUp === null ? "…" : apiUp ? "connected" : "offline"}
        </span>
        <span>·</span>
        {routes.length === 0 ? (
          <span>Select models and run to compare routes.</span>
        ) : (
          routes.map((r) => (
            <span key={r.model_id}>
              <span className="swatch" style={{ background: modelColor(models, r.model_id) }} />
              {r.model_id}: {Math.round(r.elapsed_s / 3600)} h · {r.distance_nm} nm · {r.n_tacks}T/
              {r.n_gybes}G
            </span>
          ))
        )}
      </div>
    </div>
  );
}

function modelColor(models: ModelPreset[], id: string): string {
  return models.find((m) => m.id === id)?.color ?? "#2dd4bf";
}
