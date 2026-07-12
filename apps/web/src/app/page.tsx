"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import CompareBar from "@/components/CompareBar";
import CourseEditor, { type EditMode } from "@/components/CourseEditor";
import ForecastDropdown from "@/components/ForecastDropdown";
import ForecastPicker from "@/components/ForecastPicker";
import MapControls from "@/components/MapControls";
import MapLoadOverlay, { type MapLoadKind } from "@/components/MapLoadOverlay";
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
import {
  bboxContains,
  regionBounds,
  regionCellKey,
  regionNeedsDenserReplace,
} from "@/lib/windTiles";
import {
  contiguousRunMax,
  fetchWindArea,
  floorHourIso,
  hoursAheadIso,
  mergeSeries,
  missingRange,
  seriesCoversHour,
  type WindSeries,
} from "@/lib/meteoGrid";
import type { BoatConfig, Course, ModelPreset, ModelRoute } from "@/lib/types";

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

const SERIES_MARGIN_DEG = 0.5;
/** Scrubber range; browse wind uses focus + sliding prefetch. */
const SCRUB_HORIZON_H = 120;
/** Focus hour + (N-1) ahead on land; extend by N at penultimate. */
const PREFETCH_WINDOW_H = 3;

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

function capHour(iso: string, horizonEndUtc: string): string {
  return new Date(iso).getTime() > new Date(horizonEndUtc).getTime() ? horizonEndUtc : iso;
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
  const prefetchAbortRefs = useRef<Map<string, AbortController>>(new Map());
  const prefetchKeyRef = useRef<Map<string, string>>(new Map());
  const routeAbortRefs = useRef<Map<string, AbortController>>(new Map());
  const browseCacheRef = useRef(browseCache);
  browseCacheRef.current = browseCache;

  // Drop other models' wind grids when switching tab — tiles stay on disk.
  useEffect(() => {
    setBrowseCache((prev) => {
      if (!activeModelId) return {};
      const keep = prev[activeModelId];
      if (!keep) return {};
      if (Object.keys(prev).length === 1 && prev[activeModelId]) return prev;
      return { [activeModelId]: keep };
    });
  }, [activeModelId]);

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

  const scrubHourUtc = floorHourIso(overlayTimeUtc);
  const activeStatus = activeModelId ? overlayStatus[activeModelId] : undefined;
  const hourUncovered =
    routes.length === 0 &&
    !!activeModelId &&
    selected.has(activeModelId) &&
    apiUp === true &&
    (!windSeries || !seriesCoversHour(windSeries, scrubHourUtc));
  // Show loading as soon as the scrub hour isn't covered (before the debounced fetch),
  // but stop if that hour already failed.
  const windLoading =
    activeStatus === "fetching" || (hourUncovered && activeStatus !== "error");
  const routeLoading = prepState === "preparing" || running;

  let mapLoadKind: MapLoadKind | null = null;
  let mapLoadLabel = "";
  if (routeLoading) {
    mapLoadKind = "route";
    mapLoadLabel = prepState === "preparing" ? "Preparing route data…" : "Routing…";
  } else if (windLoading && !windSeries) {
    mapLoadKind = "wind-initial";
    mapLoadLabel = activeModelId ? `Loading ${activeModelId}` : "Loading forecast";
  } else if (windLoading) {
    mapLoadKind = "wind-hour";
    mapLoadLabel = `Loading ${scrubHourUtc.slice(11, 16)} UTC`;
  }

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

  const fetchBrowseFocus = useCallback(
    (
      modelId: string,
      viewportBounds: MapBounds,
      atUtc: string,
      merge: boolean,
      onReady?: () => void,
    ) => {
      browseAbortRefs.current.get(modelId)?.abort();
      const ac = new AbortController();
      browseAbortRefs.current.set(modelId, ac);
      const hour = floorHourIso(atUtc);
      // Request the padded cell so merges stay on one grid across pans.
      const reqBounds = regionBounds(viewportBounds);
      const cacheRegion = reqBounds;
      setStatus(modelId, "fetching");
      if (modelId === activeModelId) setOverlayMsg(null);

      fetchWindArea(reqBounds, modelId, hour, hour, ac.signal)
        .then(({ series, error }) => {
          if (ac.signal.aborted) return;
          if (series) {
            setBrowseCache((prev) => {
              const existing = prev[modelId];
              const merged =
                merge && existing ? mergeSeries(existing.series, series) : series;
              const last = merged.times[merged.times.length - 1] ?? hour;
              // Chart tiles stay on disk; only keep the active model's wind in RAM.
              return {
                [modelId]: {
                  series: merged,
                  region: merge && existing ? existing.region : cacheRegion,
                  loadedEndUtc: last,
                },
              };
            });
            setStatus(modelId, "ready");
            if (modelId === activeModelId) setOverlayMsg(null);
            onReady?.();
          } else if (!merge) {
            setBrowseCache((prev) => {
              const next = { ...prev };
              delete next[modelId];
              return next;
            });
            setStatus(modelId, "error");
            if (modelId === activeModelId) {
              setOverlayMsg(error ? `Wind unavailable: ${error}` : "Wind unavailable");
            }
          } else {
            setStatus(modelId, "error");
            if (modelId === activeModelId && error) {
              setOverlayMsg(`Wind unavailable: ${error}`);
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

  const prefetchBrowseRange = useCallback(
    (modelId: string, viewportBounds: MapBounds, startUtc: string, endUtc: string) => {
      const start = floorHourIso(startUtc);
      const end = floorHourIso(endUtc);
      if (new Date(end).getTime() < new Date(start).getTime()) return;

      const entry = browseCacheRef.current[modelId];
      const gap = missingRange(entry?.series, start, end);
      if (!gap) return;

      const reqBounds = regionBounds(viewportBounds);
      const key = `${gap.start}|${gap.end}|${reqBounds.lat_min},${reqBounds.lon_min},${reqBounds.lat_max},${reqBounds.lon_max}`;
      if (prefetchKeyRef.current.get(modelId) === key) return;

      prefetchAbortRefs.current.get(modelId)?.abort();
      const ac = new AbortController();
      prefetchAbortRefs.current.set(modelId, ac);
      prefetchKeyRef.current.set(modelId, key);

      fetchWindArea(reqBounds, modelId, gap.start, gap.end, ac.signal)
        .then(({ series }) => {
          if (ac.signal.aborted || !series) return;
          setBrowseCache((prev) => {
            const existing = prev[modelId];
            if (!existing) {
              return {
                [modelId]: {
                  series,
                  region: reqBounds,
                  loadedEndUtc: series.times[series.times.length - 1] ?? gap.end,
                },
              };
            }
            const merged = mergeSeries(existing.series, series);
            return {
              [modelId]: {
                series: merged,
                region: existing.region,
                loadedEndUtc: merged.times[merged.times.length - 1] ?? existing.loadedEndUtc,
              },
            };
          });
        })
        .catch((e) => {
          if ((e as Error).name === "AbortError") return;
        })
        .finally(() => {
          if (prefetchKeyRef.current.get(modelId) === key) {
            prefetchKeyRef.current.delete(modelId);
          }
        });
    },
    [],
  );

  const scheduleBrowseWindow = useCallback(
    (
      modelId: string,
      viewportBounds: MapBounds,
      focusUtc: string,
      horizonEnd: string,
      opts?: { forceReplace?: boolean },
    ) => {
      const focus = floorHourIso(focusUtc);
      const entry = browseCacheRef.current[modelId];
      const reqRegion = regionBounds(viewportBounds);
      const inRegion = !!(entry && bboxContains(entry.region, viewportBounds, 0.05));
      const denserNeeded = !!(entry && regionNeedsDenserReplace(entry.region, reqRegion));
      const merge = inRegion && !opts?.forceReplace && !denserNeeded;
      const needFocus = !merge || !entry || !seriesCoversHour(entry.series, focus);

      const kickPrefetch = () => {
        const aheadEnd = capHour(hoursAheadIso(focus, PREFETCH_WINDOW_H - 1), horizonEnd);
        const prefetchStart = hoursAheadIso(focus, 1);
        if (new Date(prefetchStart).getTime() <= new Date(aheadEnd).getTime()) {
          prefetchBrowseRange(modelId, viewportBounds, prefetchStart, aheadEnd);
        }
      };

      if (needFocus) {
        if (!merge) {
          prefetchAbortRefs.current.get(modelId)?.abort();
          prefetchKeyRef.current.delete(modelId);
        }
        // After a cell replace, wait for focus so prefetch can't be wiped by merge=false.
        fetchBrowseFocus(modelId, viewportBounds, focus, merge, merge ? undefined : kickPrefetch);
        if (merge) queueMicrotask(kickPrefetch);
      } else {
        kickPrefetch();
      }
    },
    [fetchBrowseFocus, prefetchBrowseRange],
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

  // Browse: only the active model — wind series stay small in RAM; chart tiles are on disk.
  useEffect(() => {
    if (routes.length > 0) return;
    if (!viewport || apiUp !== true) return;
    if (!activeModelId || !selected.has(activeModelId)) return;

    const t = setTimeout(() => {
      const id = activeModelId;
      const focus = floorHourIso(overlayTimeUtc);
      const reqRegion = regionBounds(viewport);
      const entry = browseCacheRef.current[id];
      const inRegion = !!(entry && bboxContains(entry.region, viewport, 0.05));
      const denserNeeded = !!(entry && regionNeedsDenserReplace(entry.region, reqRegion));
      if (inRegion && !denserNeeded && entry && seriesCoversHour(entry.series, focus)) {
        const aheadEnd = capHour(hoursAheadIso(focus, PREFETCH_WINDOW_H - 1), horizonEndUtc);
        const prefetchStart = hoursAheadIso(focus, 1);
        if (new Date(prefetchStart).getTime() <= new Date(aheadEnd).getTime()) {
          prefetchBrowseRange(id, viewport, prefetchStart, aheadEnd);
        }
        return;
      }
      scheduleBrowseWindow(id, viewport, focus, horizonEndUtc, {
        forceReplace: !inRegion || denserNeeded,
      });
    }, 250);

    return () => clearTimeout(t);
  }, [
    viewport,
    selected,
    activeModelId,
    apiUp,
    routes.length,
    overlayTimeUtc,
    horizonEndUtc,
    scheduleBrowseWindow,
    prefetchBrowseRange,
  ]);

  // Penultimate of contiguous run → prefetch next PREFETCH_WINDOW_H hours after runMax.
  useEffect(() => {
    if (routes.length > 0) return;
    if (!viewport || !activeModelId || apiUp !== true) return;
    const entry = browseCache[activeModelId];
    if (!entry || !bboxContains(entry.region, viewport, 0.05)) return;

    const focus = floorHourIso(overlayTimeUtc);
    const runMax = contiguousRunMax(entry.series, focus);
    if (!runMax) return;
    const penultimate = hoursAheadIso(runMax, -1);
    if (focus !== penultimate) return;

    const extendStart = hoursAheadIso(runMax, 1);
    const extendEnd = capHour(hoursAheadIso(runMax, PREFETCH_WINDOW_H), horizonEndUtc);
    if (new Date(extendStart).getTime() > new Date(extendEnd).getTime()) return;
    prefetchBrowseRange(activeModelId, viewport, extendStart, extendEnd);
  }, [
    overlayTimeUtc,
    browseCache,
    viewport,
    activeModelId,
    apiUp,
    routes.length,
    horizonEndUtc,
    prefetchBrowseRange,
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
      prefetchAbortRefs.current.get(id)?.abort();
      prefetchKeyRef.current.delete(id);
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
        scheduleBrowseWindow(id, viewport, overlayTimeUtc, horizonEndUtc, {
          forceReplace: true,
        });
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
    !!activeModelId && overlayStatus[activeModelId] === "error" ? overlayMsg : null;

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
          windTimeUtc={routes.length > 0 ? scrubHourUtc : overlayTimeUtc}
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
          loading={windLoading && routes.length === 0}
          routeActive={routes.length > 0}
          leg={playbackLeg}
          playUntilUtc={activeRoute?.eta_utc ?? null}
        />
        <MapLoadOverlay kind={mapLoadKind} label={mapLoadLabel} />
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
