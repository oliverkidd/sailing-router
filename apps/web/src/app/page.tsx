"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import CompareBar from "@/components/CompareBar";
import CourseEditor, { type EditMode } from "@/components/CourseEditor";
import ForecastPicker from "@/components/ForecastPicker";
import InstrumentCluster from "@/components/InstrumentCluster";
import MapControls from "@/components/MapControls";
import ModelPicker from "@/components/ModelPicker";
import RouteDetailPanel from "@/components/RouteDetailPanel";
import RoutePlayback from "@/components/RoutePlayback";
import WindLegend from "@/components/WindLegend";
import { api } from "@/lib/api";
import {
  courseIsReady,
  currentForecastHourUtc,
  emptyCourse,
  loadStoredCourse,
  nextForecastHourUtc,
  saveStoredCourse,
  type MapBounds,
} from "@/lib/mapOverlays";
import { FORECAST_MAX_SPAN_MI, regionCellKey } from "@/lib/windTiles";
import type { BoatConfig, Course, ModelPreset, ModelRoute } from "@/lib/types";

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

const INITIAL_BOAT: BoatConfig = {
  polar_id: "",
  tack_penalty_s: 30,
  gybe_penalty_s: 20,
  upwind_twa_deg: null,
  downwind_twa_deg: null,
  leeway_coeff: 0,
  leeway_max_deg: 8,
  polar_efficiency: 1,
};

export default function Home() {
  const [apiUp, setApiUp] = useState<boolean | null>(null);
  const [availableModels, setAvailableModels] = useState<ModelPreset[]>([]);
  const [boat, setBoat] = useState<BoatConfig>(INITIAL_BOAT);
  const [course, setCourse] = useState<Course>(() => loadStoredCourse() ?? emptyCourse(nextForecastHourUtc()));
  const [editMode, setEditMode] = useState<EditMode>("none");
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string>("");
  const [overlayMsg, setOverlayMsg] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set(["gfs", "ecmwf", "icon"]));

  const [showWind, setShowWind] = useState(true);
  const [maxSpanMiles, setMaxSpanMiles] = useState<number>(Infinity);
  const [viewport, setViewport] = useState<MapBounds | null>(null);

  const [activeModelId, setActiveModelId] = useState("ecmwf");
  const [routeIndex, setRouteIndex] = useState(0);
  const [legIndex, setLegIndex] = useState(0);
  const [detailOpen, setDetailOpen] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [fitRouteKey, setFitRouteKey] = useState("");
  const [prepState, setPrepState] = useState<"idle" | "preparing" | "ready" | "error">("idle");

  const availableCacheRef = useRef<Map<string, ModelPreset[]>>(new Map());
  const availableAbortRef = useRef<AbortController | null>(null);

  const forecastEnabled = maxSpanMiles < FORECAST_MAX_SPAN_MI;

  const activeRouteIndex = routes.findIndex((r) => r.model_id === activeModelId);
  const displayRouteIndex = activeRouteIndex >= 0 ? activeRouteIndex : routeIndex;
  const activeRoute = routes[displayRouteIndex];
  const playbackLeg = activeRoute?.legs[legIndex];
  // Overlay time: current hour when no route, otherwise playback leg time.
  const windTimeUtc = playbackLeg?.t_utc ?? currentForecastHourUtc();

  const activeModelResolution = useMemo(() => {
    const m = availableModels.find((x) => x.id === activeModelId);
    return m?.resolution_deg ?? 0.25;
  }, [availableModels, activeModelId]);

  const playbackPosition = useMemo<[number, number] | null>(() => {
    if (!playbackLeg) return null;
    return playbackLeg.position;
  }, [playbackLeg]);

  const playbackHeading = useMemo(() => {
    if (!playbackLeg) return null;
    return playbackLeg.cog ?? playbackLeg.heading ?? null;
  }, [playbackLeg]);

  useEffect(() => {
    saveStoredCourse(course);
  }, [course]);

  useEffect(() => {
    if (routes.length === 0) return;
    const idx = routes.findIndex((r) => r.model_id === activeModelId);
    if (idx >= 0) setRouteIndex(idx);
  }, [activeModelId, routes]);

  // Keep selection intersecting available models when area changes.
  useEffect(() => {
    if (!forecastEnabled || availableModels.length === 0) return;
    const ids = new Set(availableModels.map((m) => m.id));
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => ids.has(id)));
      if (next.size === 0) {
        for (const id of ["gfs", "ecmwf", "icon"]) {
          if (ids.has(id)) next.add(id);
        }
        if (next.size === 0 && availableModels[0]) next.add(availableModels[0].id);
      }
      return next;
    });
    setActiveModelId((prev) => (ids.has(prev) ? prev : availableModels[0]?.id ?? prev));
  }, [availableModels, forecastEnabled]);

  const handleViewportChange = useCallback((bounds: MapBounds, span: number) => {
    setViewport(bounds);
    setMaxSpanMiles(span);
  }, []);

  // Fetch available models for the viewport region (cached per region cell).
  useEffect(() => {
    if (!forecastEnabled || !viewport || apiUp !== true) {
      if (!forecastEnabled) setAvailableModels([]);
      return;
    }
    const cell = regionCellKey(viewport);
    const cached = availableCacheRef.current.get(cell);
    if (cached) {
      setAvailableModels(cached);
      return;
    }

    const t = setTimeout(() => {
      availableAbortRef.current?.abort();
      const ac = new AbortController();
      availableAbortRef.current = ac;
      api
        .modelsAvailable(viewport)
        .then((list) => {
          if (ac.signal.aborted) return;
          availableCacheRef.current.set(cell, list);
          setAvailableModels(list);
        })
        .catch(() => {
          if (ac.signal.aborted) return;
          setAvailableModels([]);
        });
    }, 200);

    return () => clearTimeout(t);
  }, [forecastEnabled, viewport, apiUp]);

  const prepareKey = useMemo(() => {
    if (!courseIsReady(course) || !forecastEnabled) return null;
    return JSON.stringify({
      start: course.start,
      finish: course.finish,
      marks: course.marks.map((m) => m.position),
      depart: course.depart_utc,
      models: Array.from(selected).sort(),
    });
  }, [course, selected, forecastEnabled]);

  useEffect(() => {
    if (!prepareKey || apiUp !== true) {
      if (!courseIsReady(course)) setPrepState("idle");
      return;
    }

    let cancelled = false;
    setPrepState("preparing");
    setRunMsg("Preparing course data for routing…");

    api
      .prepareCourse({
        course,
        model_ids: Array.from(selected),
      })
      .then((res) => {
        if (cancelled) return;
        const ok = res.models.filter((m) => m.status === "fetched" || m.status === "cached");
        const failed = res.models.filter((m) => m.status === "error");
        if (ok.length === 0) {
          setPrepState("error");
          const detail = failed.map((m) => `${m.model_id}: ${m.message ?? "failed"}`).join("; ");
          setRunMsg(
            detail
              ? `Forecast prefetch failed — ${detail}`
              : "Forecast prefetch failed. Open-Meteo may be rate-limited; retry in a minute.",
          );
          return;
        }
        setPrepState("ready");
        let msg = `Routing data ready (${res.horizon_h}h window, ${ok.length} model field(s)).`;
        if (failed.length) {
          msg += ` Failed: ${failed.map((m) => m.model_id).join(", ")}.`;
        }
        if (res.geo === "error") msg += " Land/depth cache failed.";
        setRunMsg(msg);
      })
      .catch((e) => {
        if (cancelled) return;
        setPrepState("error");
        setRunMsg(String(e));
      });

    return () => {
      cancelled = true;
    };
  }, [prepareKey, apiUp, course, selected]);

  const handleMapClick = (lat: number, lon: number) => {
    if (editMode === "none") return;
    if (editMode === "start") setCourse((c) => ({ ...c, start: [lat, lon] }));
    else if (editMode === "finish") setCourse((c) => ({ ...c, finish: [lat, lon] }));
    else if (editMode === "mark")
      setCourse((c) => ({
        ...c,
        marks: [...c.marks, { position: [lat, lon], radius_nm: 0.3, rounding: "either", name: `M${c.marks.length + 1}` }],
      }));
  };

  const toggleModel = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      if (next.size === 1) setActiveModelId(Array.from(next)[0]);
      return next;
    });

  const runRoute = async (modelIds: string[]) => {
    if (!boat.polar_id) {
      setRunMsg("Load a polar first.");
      return;
    }
    if (!courseIsReady(course)) {
      setRunMsg("Set start and finish on the map first.");
      return;
    }
    setRunning(true);
    setRunMsg("Routing…");
    try {
      const res = await api.route({
        course,
        boat,
        model_ids: modelIds,
        time_step_s: 300,
        heading_step_deg: 7.5,
      });
      setRoutes(res.routes);
      if (res.routes.length > 0) {
        setActiveModelId(res.routes[0].model_id);
        setRouteIndex(0);
      }
      setLegIndex(0);
      setPlaying(false);
      setShowWind(true);
      setFitRouteKey(`${Date.now()}-${res.routes.length}`);
      if (res.routes.length === 0) {
        setRunMsg(
          res.warnings.length
            ? res.warnings.join(" ")
            : "No routes computed — forecast data could not be loaded.",
        );
      } else if (res.warnings.length) {
        console.warn("[route] warnings:", res.warnings);
        setRunMsg(`${res.routes.length} route(s) — ${res.warnings.join(" ")}`);
      } else {
        setRunMsg(`${res.routes.length} route(s) computed.`);
      }
    } catch (e) {
      console.error("[route] failed:", e);
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
  }, []);

  return (
    <div id="app-shell">
      <aside className="rail">
        <h1>Sailing Weather Router</h1>
        <div className="subtitle">Zoom to area · pick forecast · route</div>

        <div className="rail-section">
          <h2>Course</h2>
          <CourseEditor
            course={course}
            onCourseChange={setCourse}
            editMode={editMode}
            onEditMode={setEditMode}
            courseReady={courseIsReady(course)}
          />
        </div>

        <div className="rail-section">
          <h2>Boat</h2>
          <BoatConfigPanel config={boat} onConfigChange={setBoat} />
        </div>

        <div className="rail-section">
          <h2>Models</h2>
          <ModelPicker
            models={availableModels}
            selected={selected}
            onToggle={toggleModel}
            onRun={() => runRoute(Array.from(selected))}
            running={running}
            forecastEnabled={forecastEnabled}
          />
          {runMsg && <div style={{ color: "var(--muted)", fontSize: 11, marginTop: 6 }}>{runMsg}</div>}
          {prepState === "preparing" && (
            <div style={{ color: "var(--muted)", fontSize: 10, marginTop: 4 }}>Prefetching routing data…</div>
          )}
        </div>
      </aside>

      <div className="map-area">
        <MapView
          routes={routes}
          course={course}
          showWind={showWind}
          forecastEnabled={forecastEnabled}
          windModelId={activeModelId}
          windModelResolutionDeg={activeModelResolution}
          windTimeUtc={windTimeUtc}
          playbackPosition={playbackPosition}
          playbackHeading={playbackHeading}
          fitRouteKey={fitRouteKey}
          onMapClick={editMode === "none" ? undefined : handleMapClick}
          onRouteSelect={setActiveModelId}
          onOverlayError={setOverlayMsg}
          onViewportChange={handleViewportChange}
        />
        {playbackLeg && <InstrumentCluster leg={playbackLeg} />}
        {overlayMsg && (
          <div
            style={{
              position: "absolute",
              top: 12,
              left: "50%",
              transform: "translateX(-50%)",
              zIndex: 5,
              maxWidth: "min(90%, 520px)",
              padding: "8px 12px",
              borderRadius: 8,
              background: "rgba(127, 29, 29, 0.92)",
              color: "#fecaca",
              fontSize: 11,
              lineHeight: 1.4,
              border: "1px solid rgba(248, 113, 113, 0.4)",
              pointerEvents: "none",
            }}
          >
            {overlayMsg}
          </div>
        )}
        <MapControls
          toggles={{ showWind }}
          onChange={(patch) => {
            if (patch.showWind !== undefined) setShowWind(patch.showWind);
          }}
        />
        <WindLegend visible={showWind && forecastEnabled} />
        <ForecastPicker
          routes={routes}
          models={availableModels}
          selectedModels={selected}
          activeModelId={activeModelId}
          onActiveModel={setActiveModelId}
          enabled={forecastEnabled}
        />
      </div>

      <RouteDetailPanel
        routes={routes}
        routeIndex={displayRouteIndex}
        legIndex={legIndex}
        open={detailOpen}
        onToggle={() => setDetailOpen((o) => !o)}
        onRouteIndex={(i) => {
          setRouteIndex(i);
          if (routes[i]) setActiveModelId(routes[i].model_id);
        }}
      />

      <div className="compare-bar">
        <span style={{ flexShrink: 0 }}>
          <span className={`status-dot ${apiUp ? "ok" : "down"}`} />
          API {apiUp === null ? "…" : apiUp ? "connected" : "offline"}
          {Number.isFinite(maxSpanMiles) && (
            <> · {Math.round(maxSpanMiles)} mi</>
          )}
        </span>
        <span style={{ flexShrink: 0 }}>·</span>
        <CompareBar routes={routes} />
        <RoutePlayback
          routes={routes}
          routeIndex={displayRouteIndex}
          legIndex={legIndex}
          onLegIndex={setLegIndex}
          playing={playing}
          onPlaying={setPlaying}
        />
      </div>
    </div>
  );
}
