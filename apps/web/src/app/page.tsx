"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import CompareBar from "@/components/CompareBar";
import ModelPicker from "@/components/ModelPicker";
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
  const [selected, setSelected] = useState<Set<string>>(new Set(["ecmwf"]));

  const toggleModel = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

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
          <ModelPicker
            models={models}
            selected={selected}
            onToggle={toggleModel}
            useCurrents={useCurrents}
            useTerrain={useTerrain}
            onCurrents={setUseCurrents}
            onTerrain={setUseTerrain}
            onRun={() => runRoute(Array.from(selected))}
            running={running}
          />
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
        <span style={{ flexShrink: 0 }}>
          <span className={`status-dot ${apiUp ? "ok" : "down"}`} />
          API {apiUp === null ? "…" : apiUp ? "connected" : "offline"}
        </span>
        <span style={{ flexShrink: 0 }}>·</span>
        <CompareBar routes={routes} />
      </div>
    </div>
  );
}
