"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import CompareBar from "@/components/CompareBar";
import CourseEditor, { type EditMode } from "@/components/CourseEditor";
import ForecastDropdown from "@/components/ForecastDropdown";
import ForecastPicker from "@/components/ForecastPicker";
import InstrumentCluster from "@/components/InstrumentCluster";
import MapControls from "@/components/MapControls";
import RouteBuildProgress, { type ModelStatus } from "@/components/RouteBuildProgress";
import RouteDetailPanel from "@/components/RouteDetailPanel";
import RouteReview from "@/components/RouteReview";
import TimeScrubber from "@/components/TimeScrubber";
import WindLegend from "@/components/WindLegend";
import { api } from "@/lib/api";
import {
  courseIsReady,
  currentForecastHourUtc,
  emptyCourse,
  ensureFutureDepart,
  legAtTime,
  loadStoredCourse,
  saveStoredCourse,
  type MapBounds,
} from "@/lib/mapOverlays";
import { bboxContains, regionCellKey } from "@/lib/windTiles";
import { fetchWindArea, mergeSeries, type WindSeries } from "@/lib/meteoGrid";
import type { BoatConfig, Course, ModelPreset, ModelRoute } from "@/lib/types";

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

const SERIES_MARGIN_DEG = 0.5;
const BROWSE_DAY_H = 24;
const SCRUB_HORIZON_H = 120;
const LAZY_EXTEND_LEAD_H = 6;

type WindCacheEntry = {
  series: WindSeries;
  region: MapBounds;
  loadedEndUtc: string;
};

function courseBBox(course: Course, margin = SERIES_MARGIN_DEG): MapBounds {
  const lats = [course.start[0], course.finish[0], ...course.marks.map((m) => m.position[0])];
  const lons = [course.start[1], course.finish[1], ...course.marks.map((m) => m.position[1])];
  return {
    lat_min: Math.min(...lats) - margin,
    lat_max: Math.max(...lats) + margin,
    lon_min: Math.min(...lons) - margin,
    lon_max: Math.max(...lons) + margin,
  };
}

function routeTimeWindow(route: ModelRoute, departUtc: string): { start: string; end: string } {
  const last = route.legs[route.legs.length - 1];
  return {
    start: departUtc,
    end: last?.t_utc ?? departUtc,
  };
}

function hoursAheadIso(fromIso: string, hours: number): string {
  const d = new Date(fromIso);
  d.setUTCHours(d.getUTCHours() + hours);
  return d.toISOString();
}

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

const INITIAL_COURSE = emptyCourse("1970-01-01T00:00:00.000Z");

export default function Home() {
  const [apiUp, setApiUp] = useState<boolean | null>(null);
  const [availableModels, setAvailableModels] = useState<ModelPreset[]>([]);
  const [boat, setBoat] = useState<BoatConfig>(INITIAL_BOAT);
  const [course, setCourse] = useState<Course>(INITIAL_COURSE);
  const [courseReadyToPersist, setCourseReadyToPersist] = useState(false);
  const [editMode, setEditMode] = useState<EditMode>("none");
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string>("");
  const [overlayMsg, setOverlayMsg] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [showWind, setShowWind] = useState(true);
  const [viewport, setViewport] = useState<MapBounds | null>(null);

  const [activeModelId, setActiveModelId] = useState("");
  const [routeIndex, setRouteIndex] = useState(0);
  const [legIndex, setLegIndex] = useState(0);
  const [detailOpen, setDetailOpen] = useState(false);
  const [fitRouteKey, setFitRouteKey] = useState("");
  const [prepState, setPrepState] = useState<"idle" | "preparing" | "ready" | "error">("idle");
  const [browseCache, setBrowseCache] = useState<Record<string, WindCacheEntry>>({});
  const [routeCache, setRouteCache] = useState<Record<string, WindSeries>>({});
  const [overlayStatus, setOverlayStatus] = useState<Record<string, ModelStatus>>({});
  const [overlayTimeUtc, setOverlayTimeUtc] = useState(currentForecastHourUtc());
  const [horizonStartUtc, setHorizonStartUtc] = useState(currentForecastHourUtc());

  const availableCacheRef = useRef<Map<string, ModelPreset[]>>(new Map());
  const availableAbortRef = useRef<AbortController | null>(null);
  const browseAbortRefs = useRef<Map<string, AbortController>>(new Map());
  const routeAbortRefs = useRef<Map<string, AbortController>>(new Map());
  const lazyAbortRef = useRef<AbortController | null>(null);
  const browseCacheRef = useRef(browseCache);
  browseCacheRef.current = browseCache;

  const horizonEndUtc = useMemo(
    () => hoursAheadIso(horizonStartUtc, SCRUB_HORIZON_H),
    [horizonStartUtc],
  );

  const activeRouteIndex = routes.findIndex((r) => r.model_id === activeModelId);
  const displayRouteIndex = activeRouteIndex >= 0 ? activeRouteIndex : routeIndex;
  const activeRoute = routes[displayRouteIndex];
  const windSeries =
    (activeModelId && routeCache[activeModelId]) ||
    (activeModelId && browseCache[activeModelId]?.series) ||
    null;

  const activeFetching =
    !!activeModelId && overlayStatus[activeModelId] === "fetching" && !windSeries;

  const boatAtTime = useMemo(() => {
    if (!activeRoute?.legs?.length) return null;
    return legAtTime(activeRoute.legs, overlayTimeUtc);
  }, [activeRoute, overlayTimeUtc]);

  const playbackPosition = boatAtTime?.position ?? null;
  const playbackHeading = boatAtTime?.heading ?? null;
  const playbackLeg = boatAtTime?.leg ?? null;

  useEffect(() => {
    if (boatAtTime) setLegIndex(boatAtTime.index);
  }, [boatAtTime]);

  useEffect(() => {
    const stored = loadStoredCourse();
    const now = currentForecastHourUtc();
    if (stored) {
      setCourse({
        ...stored,
        depart_utc: ensureFutureDepart(stored.depart_utc),
      });
    } else {
      setCourse(emptyCourse(now));
    }
    setOverlayTimeUtc(now);
    setHorizonStartUtc(now);
    setCourseReadyToPersist(true);
  }, []);

  useEffect(() => {
    if (!courseReadyToPersist) return;
    saveStoredCourse(course);
  }, [course, courseReadyToPersist]);

  const courseRouteKey = useMemo(
    () =>
      JSON.stringify({
        start: course.start,
        finish: course.finish,
        marks: course.marks.map((m) => m.position),
        depart: course.depart_utc,
      }),
    [course.start, course.finish, course.marks, course.depart_utc],
  );

  useEffect(() => {
    setRouteCache({});
    setRoutes([]);
    setLegIndex(0);
    setPrepState("idle");
    setRunMsg("");
  }, [courseRouteKey]);

  useEffect(() => {
    if (routes.length === 0) return;
    const idx = routes.findIndex((r) => r.model_id === activeModelId);
    if (idx >= 0) setRouteIndex(idx);
  }, [activeModelId, routes]);

  const setStatus = useCallback((id: string, status: ModelStatus) => {
    setOverlayStatus((prev) => ({ ...prev, [id]: status }));
  }, []);

  const fetchBrowseModel = useCallback(
    (modelId: string, bounds: MapBounds) => {
      browseAbortRefs.current.get(modelId)?.abort();
      const ac = new AbortController();
      browseAbortRefs.current.set(modelId, ac);
      const start = currentForecastHourUtc();
      const end = hoursAheadIso(start, BROWSE_DAY_H);
      setStatus(modelId, "fetching");
      if (modelId === activeModelId) setOverlayMsg(null);

      fetchWindArea(bounds, modelId, start, end, ac.signal)
        .then(({ series, error }) => {
          if (ac.signal.aborted) return;
          if (series) {
            setBrowseCache((prev) => ({
              ...prev,
              [modelId]: {
                series,
                region: series.bbox ?? bounds,
                loadedEndUtc: series.times[series.times.length - 1] ?? end,
              },
            }));
            setStatus(modelId, "ready");
            if (modelId === activeModelId) setOverlayMsg(null);
          } else {
            setBrowseCache((prev) => {
              const next = { ...prev };
              delete next[modelId];
              return next;
            });
            setStatus(modelId, "error");
            if (modelId === activeModelId) {
              setOverlayMsg(error ? `Wind unavailable: ${error}` : "Wind unavailable");
            }
          }
        })
        .catch((e) => {
          if ((e as Error).name === "AbortError") return;
          if (ac.signal.aborted) return;
          setStatus(modelId, "error");
          if (modelId === activeModelId) {
            setOverlayMsg(`Wind unavailable: ${String(e)}`);
          }
        });
    },
    [activeModelId, setStatus],
  );

  // Load route-window series into routeCache per route model (no refetch on tab switch).
  useEffect(() => {
    if (routes.length === 0 || !courseIsReady(course) || apiUp !== true) return;

    const bbox = courseBBox(course);
    for (const route of routes) {
      if (!route.legs.length) continue;
      const modelId = route.model_id;
      if (routeCache[modelId]) continue;

      routeAbortRefs.current.get(modelId)?.abort();
      const ac = new AbortController();
      routeAbortRefs.current.set(modelId, ac);
      const { start, end } = routeTimeWindow(route, course.depart_utc);
      setStatus(modelId, "fetching");

      fetchWindArea(bbox, modelId, start, end, ac.signal)
        .then(({ series, error }) => {
          if (ac.signal.aborted) return;
          if (series) {
            setRouteCache((prev) => ({ ...prev, [modelId]: series }));
            setStatus(modelId, "ready");
          } else {
            setStatus(modelId, "error");
            if (error) console.warn("[route-series]", modelId, error);
          }
        })
        .catch((e) => {
          if ((e as Error).name === "AbortError") return;
          if (!ac.signal.aborted) setStatus(modelId, "error");
        });
    }

    return () => {
      for (const ac of routeAbortRefs.current.values()) ac.abort();
    };
    // Intentionally omit routeCache from deps — we gate on miss inside the loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes, courseRouteKey, course, apiUp, setStatus]);

  // Browse: for each selected model, fetch when missing or viewport left cached region.
  useEffect(() => {
    if (routes.length > 0) return;
    if (!viewport || apiUp !== true) return;
    if (selected.size === 0) return;

    const t = setTimeout(() => {
      for (const id of selected) {
        const entry = browseCacheRef.current[id];
        if (entry && bboxContains(entry.region, viewport, 0.05)) continue;
        fetchBrowseModel(id, viewport);
      }
    }, 250);

    return () => clearTimeout(t);
  }, [viewport, selected, apiUp, routes.length, fetchBrowseModel]);

  // Lazy-extend active browse model near loaded edge.
  useEffect(() => {
    if (routes.length > 0) return;
    if (!viewport || !activeModelId || apiUp !== true) return;
    const entry = browseCache[activeModelId];
    if (!entry) return;

    const cur = new Date(overlayTimeUtc).getTime();
    const loadedEnd = new Date(entry.loadedEndUtc).getTime();
    const horizonEnd = new Date(horizonEndUtc).getTime();
    if (cur < loadedEnd - LAZY_EXTEND_LEAD_H * 3600 * 1000) return;
    if (loadedEnd >= horizonEnd - 3600 * 1000) return;

    const start = entry.loadedEndUtc;
    const end = hoursAheadIso(start, BROWSE_DAY_H);
    lazyAbortRef.current?.abort();
    const ac = new AbortController();
    lazyAbortRef.current = ac;
    fetchWindArea(viewport, activeModelId, start, end, ac.signal)
      .then(({ series }) => {
        if (ac.signal.aborted || !series) return;
        setBrowseCache((prev) => {
          const curEntry = prev[activeModelId];
          if (!curEntry) return prev;
          return {
            ...prev,
            [activeModelId]: {
              series: mergeSeries(curEntry.series, series),
              region: curEntry.region,
              loadedEndUtc: series.times[series.times.length - 1] ?? end,
            },
          };
        });
      })
      .catch((e) => {
        if ((e as Error).name === "AbortError") return;
      });

    return () => ac.abort();
  }, [
    overlayTimeUtc,
    browseCache,
    viewport,
    activeModelId,
    apiUp,
    routes.length,
    horizonEndUtc,
  ]);

  // Drop selection entries that are no longer available for this area (don't auto-add).
  useEffect(() => {
    if (availableModels.length === 0) return;
    const ids = new Set(availableModels.map((m) => m.id));
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => ids.has(id)));
      if (next.size === prev.size && [...prev].every((id) => next.has(id))) return prev;
      return next;
    });
  }, [availableModels]);

  useEffect(() => {
    if (!activeModelId) return;
    if (selected.has(activeModelId)) return;
    setActiveModelId(selected.values().next().value ?? "");
  }, [selected, activeModelId]);

  useEffect(() => {
    if (!activeModelId) {
      setOverlayMsg(null);
      return;
    }
    if (overlayStatus[activeModelId] !== "error") setOverlayMsg(null);
  }, [activeModelId, overlayStatus]);

  const handleViewportChange = useCallback((bounds: MapBounds, _span: number) => {
    setViewport(bounds);
  }, []);

  useEffect(() => {
    if (!viewport || apiUp !== true) return;
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
  }, [viewport, apiUp]);

  const handleMapClick = (lat: number, lon: number) => {
    if (editMode === "none") return;
    if (editMode === "start") setCourse((c) => ({ ...c, start: [lat, lon] }));
    else if (editMode === "finish") setCourse((c) => ({ ...c, finish: [lat, lon] }));
    else if (editMode === "mark")
      setCourse((c) => ({
        ...c,
        marks: [
          ...c.marks,
          {
            position: [lat, lon],
            radius_nm: 0.3,
            rounding: "either",
            name: `M${c.marks.length + 1}`,
          },
        ],
      }));
  };

  const toggleModel = (id: string) => {
    const wasSelected = selected.has(id);
    const next = new Set(selected);
    if (wasSelected) {
      next.delete(id);
      browseAbortRefs.current.get(id)?.abort();
      setBrowseCache((c) => {
        const n = { ...c };
        delete n[id];
        return n;
      });
      setOverlayStatus((s) => {
        const n = { ...s };
        delete n[id];
        return n;
      });
      if (activeModelId === id) {
        setActiveModelId(next.values().next().value ?? "");
      }
    } else {
      next.add(id);
      setActiveModelId(id);
      if (viewport && apiUp === true && routes.length === 0) {
        fetchBrowseModel(id, viewport);
      }
    }
    setSelected(next);
  };

  const runRoute = async () => {
    const modelIds = Array.from(selected);
    if (!boat.polar_id) {
      setPrepState("error");
      setRunMsg("Load a polar first.");
      return;
    }
    if (!courseIsReady(course)) {
      setPrepState("error");
      setRunMsg("Set start and finish on the map first.");
      return;
    }
    if (modelIds.length === 0) {
      setPrepState("error");
      setRunMsg("Choose at least one forecast on the map.");
      return;
    }

    setRunning(true);
    setPrepState("preparing");
    setRunMsg("");
    try {
      const prep = await api.prepareCourse({ course, model_ids: modelIds });
      const ok = prep.models.filter((m) => m.status === "fetched" || m.status === "cached");
      if (ok.length === 0) {
        setPrepState("error");
        const detail = prep.models
          .filter((m) => m.status === "error")
          .map((m) => `${m.model_id}: ${m.message ?? "failed"}`)
          .join("; ");
        setRunMsg(detail || "Forecast prefetch failed.");
        return;
      }

      setPrepState("ready");
      setRunMsg("Routing…");
      const res = await api.route({
        course,
        boat,
        model_ids: modelIds,
        time_step_s: 300,
        heading_step_deg: 7.5,
      });
      setRoutes(res.routes);
      setRouteCache({});
      if (res.routes.length > 0) {
        setActiveModelId(res.routes[0].model_id);
        setRouteIndex(0);
        setOverlayTimeUtc(course.depart_utc);
        setRunMsg("");
        if (res.warnings.length) console.warn("[route] warnings:", res.warnings);
      } else {
        setPrepState("error");
        setRunMsg(
          res.warnings.length
            ? res.warnings.join(" ")
            : "No routes computed — forecast data could not be loaded.",
        );
      }
      setLegIndex(0);
      setShowWind(true);
      setFitRouteKey(`${Date.now()}-${res.routes.length}`);
    } catch (e) {
      console.error("[route] failed:", e);
      setPrepState("error");
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

  const activeError =
    !!activeModelId &&
    overlayStatus[activeModelId] === "error" &&
    !windSeries
      ? overlayMsg
      : null;

  return (
    <div id="app-shell">
      <aside className="rail">
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
          <BoatConfigPanel
            config={boat}
            onConfigChange={setBoat}
            minDepthM={course.min_depth_m}
            onMinDepthChange={(m) => setCourse((c) => ({ ...c, min_depth_m: m }))}
          />
        </div>

        <div className="rail-section">
          <h2>Route</h2>
          <RouteReview
            course={course}
            selected={selected}
            models={availableModels}
            running={running}
            prepState={prepState}
            onRun={runRoute}
          />
        </div>
      </aside>

      <div className="map-area">
        <MapView
          routes={routes}
          course={course}
          showWind={showWind}
          windTimeUtc={overlayTimeUtc}
          windSeries={windSeries}
          playbackPosition={playbackPosition}
          playbackHeading={playbackHeading}
          fitRouteKey={fitRouteKey}
          onMapClick={editMode === "none" ? undefined : handleMapClick}
          onRouteSelect={setActiveModelId}
          onOverlayError={setOverlayMsg}
          onViewportChange={handleViewportChange}
        />
        <TimeScrubber
          overlayTimeUtc={overlayTimeUtc}
          onTime={setOverlayTimeUtc}
          horizonStartUtc={horizonStartUtc}
          horizonEndUtc={horizonEndUtc}
        />
        {playbackLeg && <InstrumentCluster leg={playbackLeg} />}
        {activeFetching && (
          <div className="map-load-chip">Loading {activeModelId}…</div>
        )}
        {activeError && <div className="map-error-banner">{activeError}</div>}
        <MapControls
          toggles={{ showWind }}
          onChange={(patch) => {
            if (patch.showWind !== undefined) setShowWind(patch.showWind);
          }}
        />
        <WindLegend visible={showWind && !!windSeries} />
        <div className="forecast-dock">
          <ForecastPicker
            routes={routes}
            models={availableModels}
            selectedModels={selected}
            activeModelId={activeModelId}
            onActiveModel={setActiveModelId}
            overlayStatus={overlayStatus}
          />
          <ForecastDropdown
            models={availableModels}
            selected={selected}
            onToggle={toggleModel}
          />
        </div>
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
        <RouteBuildProgress
          prepState={prepState}
          running={running}
          routeCount={routes.length}
          message={runMsg || undefined}
        />
        <CompareBar routes={routes} />
      </div>
    </div>
  );
}
