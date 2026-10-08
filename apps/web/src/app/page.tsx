"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import CourseEditor, { type EditMode } from "@/components/CourseEditor";
import LiveRacePanel, { useLivePosition } from "@/components/LiveRacePanel";
import OfflinePack from "@/components/OfflinePack";
import ForecastDropdown from "@/components/ForecastDropdown";
import ForecastPicker from "@/components/ForecastPicker";
import MapControls from "@/components/MapControls";
import MapLoadOverlay, { type MapLoadKind } from "@/components/MapLoadOverlay";
import { type ModelStatus } from "@/components/RouteBuildProgress";
import {
  applyRouteEvent,
  initialRouteSteps,
  resetModelSteps,
} from "@/components/RouteProgress";
import RouteDetailPanel from "@/components/RouteDetailPanel";
import RouteModelTabs from "@/components/RouteModelTabs";
import RouteReview, { formatSummaryClipboard } from "@/components/RouteReview";
import TimeScrubber from "@/components/TimeScrubber";
import WindLegend from "@/components/WindLegend";
import ZoomDebugChip from "@/components/ZoomDebugChip";
import { api } from "@/lib/api";
import { pinChartCache } from "@/lib/deviceCache";
import {
  clearStoredRouteSession,
  courseIsReady,
  cumulativeDistanceNm,
  currentForecastHourUtc,
  emptyCourse,
  ensureFutureDepart,
  isPointSet,
  legAtTime,
  loadStoredCourse,
  loadStoredRouteSession,
  saveStoredCourse,
  saveStoredRouteSession,
  withSequentialMarkNames,
  type MapBounds,
} from "@/lib/mapOverlays";
import {
  bboxContains,
  browseFetchZoom,
  regionCellKey,
  tierForZoom,
} from "@/lib/windTiles";
import {
  contiguousRunMax,
  fetchWindArea,
  fetchWindAreaStream,
  floorHourIso,
  hoursAheadIso,
  mergeSeries,
  missingRange,
  seriesCoversHour,
  type WindSeries,
} from "@/lib/meteoGrid";
import { applyWindEvent, initialWindSteps } from "@/lib/windLoadProgress";
import type {
  BoatConfig,
  Course,
  ModelPreset,
  ModelRoute,
  RouteProgressStep,
} from "@/lib/types";

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

const SERIES_MARGIN_DEG = 0.35;
/** Scrubber range; browse wind uses focus + sliding prefetch. */
const SCRUB_HORIZON_H = 120;
/** Focus hour + (N-1) ahead on land; extend by N at penultimate. */
const PREFETCH_WINDOW_H = 3;

type WindCacheEntry = {
  series: WindSeries;
  region: MapBounds;
  loadedEndUtc: string;
  /** Display tier of the cached series (coarse/mid/fine/ultra). */
  tier: string;
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
  const [sessionHydrated, setSessionHydrated] = useState(false);
  const skipNextRouteClear = useRef(true);
  const courseForRunRef = useRef(course);
  const [live, setLive] = useState(false);
  const [liveFollowNow, setLiveFollowNow] = useState(true);
  const [nowUtc, setNowUtc] = useState(() => new Date().toISOString());
  const { fix: liveFix, error: liveError } = useLivePosition(live);
  const [editMode, setEditMode] = useState<EditMode>("none");
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string>("");
  const [overlayMsg, setOverlayMsg] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [showWind, setShowWind] = useState(true);
  const [showDepth, setShowDepth] = useState(true);
  const [viewport, setViewport] = useState<MapBounds | null>(null);
  const [mapZoom, setMapZoom] = useState(5);
  /** Browse-only: WindNinja terrain downscaling (always on for routing). */
  const [windNinjaOpt, setWindNinjaOpt] = useState(false);

  const [activeModelId, setActiveModelId] = useState("");
  const [routeIndex, setRouteIndex] = useState(0);
  const [detailOpen, setDetailOpen] = useState(false);
  const [fitRouteKey, setFitRouteKey] = useState("");
  const [prepState, setPrepState] = useState<
    "idle" | "preparing" | "ready" | "error" | "warning"
  >("idle");
  const [routeModal, setRouteModal] = useState<"hidden" | "running" | "error" | "done">("hidden");
  const [routeSteps, setRouteSteps] = useState<RouteProgressStep[]>([]);
  const [windSteps, setWindSteps] = useState<RouteProgressStep[]>([]);
  const [routeWarnings, setRouteWarnings] = useState<string[]>([]);
  /** Models that failed / unfinished — show per-card Retry. */
  const [retryableModelIds, setRetryableModelIds] = useState<string[]>([]);
  /** Successful routes held while other models still need retry. */
  const pendingRoutesRef = useRef<ModelRoute[]>([]);
  const routeRunModelIdsRef = useRef<string[]>([]);
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
  // Skip in route review so tab flips don't touch browse cache.
  useEffect(() => {
    if (routes.length > 0) return;
    setBrowseCache((prev) => {
      if (!activeModelId) return {};
      const keep = prev[activeModelId];
      if (!keep) return {};
      if (Object.keys(prev).length === 1 && prev[activeModelId]) return prev;
      return { [activeModelId]: keep };
    });
  }, [activeModelId, routes.length]);

  const horizonEndUtc = useMemo(
    () => hoursAheadIso(horizonStartUtc, SCRUB_HORIZON_H),
    [horizonStartUtc],
  );

  const activeRouteIndex = routes.findIndex((r) => r.model_id === activeModelId);
  const displayRouteIndex = activeRouteIndex >= 0 ? activeRouteIndex : routeIndex;
  const activeRoute = routes[displayRouteIndex];
  const windSeries =
    (activeModelId && routeCache[activeModelId]) ||
    (routes.length === 0 && activeModelId && browseCache[activeModelId]?.series) ||
    null;

  const scrubHourUtc = floorHourIso(overlayTimeUtc);
  const activeStatus = activeModelId ? overlayStatus[activeModelId] : undefined;
  const mapMode: "browse" | "routeReview" =
    routes.length > 0 || routeModal === "running" || routeModal === "error"
      ? "routeReview"
      : "browse";
  const inRouteReview = routes.length > 0;
  const hourUncovered =
    mapMode === "browse" &&
    !!activeModelId &&
    selected.has(activeModelId) &&
    apiUp === true &&
    (!windSeries || !seriesCoversHour(windSeries, scrubHourUtc));
  // Never show wind load overlay in route review — series are prefetched silently.
  const windLoading =
    mapMode === "browse" &&
    (activeStatus === "fetching" || (hourUncovered && activeStatus !== "error"));
  const routeLoading = routeModal !== "hidden";

  const modelLabels = useMemo(
    () => Object.fromEntries(availableModels.map((m) => [m.id, m.label])),
    [availableModels],
  );

  const routeHorizonEndUtc = useMemo(() => {
    if (routes.length === 0) return horizonEndUtc;
    let maxEta = course.depart_utc;
    for (const r of routes) {
      if (r.eta_utc && new Date(r.eta_utc).getTime() > new Date(maxEta).getTime()) {
        maxEta = r.eta_utc;
      }
    }
    return maxEta;
  }, [routes, course.depart_utc, horizonEndUtc]);

  let mapLoadKind: MapLoadKind | null = null;
  let mapLoadLabel = "";
  if (routeLoading) {
    mapLoadKind = "route";
    mapLoadLabel = routeModal === "done" ? "Route ready" : "Computing route…";
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

  const planNow = useMemo(() => {
    if (!live || !activeRoute?.legs?.length) return null;
    return legAtTime(activeRoute.legs, nowUtc);
  }, [live, activeRoute, nowUtc]);

  useEffect(() => {
    if (!live) return;
    const tick = () => setNowUtc(new Date().toISOString());
    tick();
    const id = window.setInterval(tick, 5000);
    return () => window.clearInterval(id);
  }, [live]);

  useEffect(() => {
    if (!live || !liveFollowNow) return;
    setOverlayTimeUtc(nowUtc);
  }, [live, liveFollowNow, nowUtc]);

  useEffect(() => {
    if (!live || !liveFix) return;
    setCourse((current) => {
      if (isPointSet(current.start)) return current;
      return { ...current, start: liveFix.position };
    });
  }, [live, liveFix]);

  const playbackPosition = boatAtTime?.position ?? null;
  const playbackHeading = boatAtTime?.heading ?? null;
  const playbackLeg = boatAtTime?.leg ?? null;
  const playbackDistanceNm = useMemo(() => {
    if (!activeRoute?.legs?.length || !boatAtTime) return 0;
    return cumulativeDistanceNm(activeRoute.legs, boatAtTime.index, boatAtTime.position);
  }, [activeRoute, boatAtTime]);

  const courseHasPoints =
    isPointSet(course.start) || isPointSet(course.finish) || course.marks.length > 0;

  useEffect(() => {
    const stored = loadStoredCourse();
    const now = currentForecastHourUtc();
    const nextCourse = stored
      ? {
          ...stored,
          marks: withSequentialMarkNames(stored.marks ?? []),
          depart_utc: ensureFutureDepart(stored.depart_utc),
        }
      : emptyCourse(now);
    setCourse(nextCourse);

    const key = JSON.stringify({
      start: nextCourse.start,
      finish: nextCourse.finish,
      marks: nextCourse.marks.map((m) => m.position),
      depart: nextCourse.depart_utc,
    });
    const session = loadStoredRouteSession();
    if (session && session.courseKey === key && session.routes.length > 0) {
      setRoutes(session.routes);
      setRouteWarnings(session.warnings ?? []);
      setActiveModelId(session.ui.activeModelId || session.routes[0].model_id);
      setRouteIndex(session.ui.routeIndex ?? 0);
      setOverlayTimeUtc(session.ui.overlayTimeUtc || nextCourse.depart_utc);
      setHorizonStartUtc(session.ui.horizonStartUtc || nextCourse.depart_utc);
      setDetailOpen(Boolean(session.ui.detailOpen));
      setPrepState("ready");
      skipNextRouteClear.current = true;
    } else {
      setOverlayTimeUtc(now);
      setHorizonStartUtc(now);
      if (session) clearStoredRouteSession();
      skipNextRouteClear.current = true;
    }
    setCourseReadyToPersist(true);
    setSessionHydrated(true);
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
    if (!sessionHydrated) return;
    if (skipNextRouteClear.current) {
      skipNextRouteClear.current = false;
      return;
    }
    clearStoredRouteSession();
    setRouteCache({});
    setRoutes([]);
    setPrepState("idle");
    setRunMsg("");
    setRouteSteps([]);
    setRouteWarnings([]);
    setRouteModal("hidden");
  }, [courseRouteKey, sessionHydrated]);

  useEffect(() => {
    if (!sessionHydrated || routes.length === 0) return;
    saveStoredRouteSession({
      v: 1,
      courseKey: courseRouteKey,
      savedAt: new Date().toISOString(),
      routes,
      warnings: routeWarnings,
      ui: {
        activeModelId,
        routeIndex,
        overlayTimeUtc,
        horizonStartUtc,
        detailOpen,
      },
    });
  }, [
    sessionHydrated,
    routes,
    routeWarnings,
    activeModelId,
    routeIndex,
    overlayTimeUtc,
    horizonStartUtc,
    detailOpen,
    courseRouteKey,
  ]);

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
      zoom?: number,
      terrain?: boolean,
    ) => {
      browseAbortRefs.current.get(modelId)?.abort();
      const ac = new AbortController();
      browseAbortRefs.current.set(modelId, ac);
      const hour = floorHourIso(atUtc);
      // First request covers focus..(focus + PREFETCH_WINDOW_H - 1) in one call.
      const endHour = hoursAheadIso(hour, PREFETCH_WINDOW_H - 1);
      // Send raw viewport — server pads once and returns the display window.
      const reqBounds = viewportBounds;
      const useTerrain = terrain ?? windNinjaOpt;
      const z = zoom ?? browseFetchZoom(useTerrain);
      setStatus(modelId, "fetching");
      if (modelId === activeModelId) setOverlayMsg(null);

      const cold = !browseCacheRef.current[modelId]?.series;
      if (cold) {
        setWindSteps(initialWindSteps(useTerrain));
      }

      const fetchPromise = cold
        ? fetchWindAreaStream(
            reqBounds,
            modelId,
            hour,
            endHour,
            (event) => {
              setWindSteps((prev) => applyWindEvent(prev, event));
            },
            ac.signal,
            z,
            useTerrain,
          )
        : fetchWindArea(reqBounds, modelId, hour, endHour, ac.signal, z, useTerrain);

      fetchPromise
        .then(({ series, error }) => {
          if (ac.signal.aborted) return;
          if (series) {
            const seriesTier = series.zoom_tier ?? tierForZoom(z);
            setBrowseCache((prev) => {
              const existing = prev[modelId];
              const merged =
                merge && existing ? mergeSeries(existing.series, series) : series;
              const last = merged.times[merged.times.length - 1] ?? hour;
              // Cache region = returned series bbox (what we can render).
              const cacheRegion = merged.bbox ?? series.bbox ?? reqBounds;
              return {
                [modelId]: {
                  series: merged,
                  region: merge && existing ? existing.region : cacheRegion,
                  loadedEndUtc: last,
                  tier: merge && existing ? existing.tier : seriesTier,
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
    [activeModelId, setStatus, windNinjaOpt],
  );

  const prefetchBrowseRange = useCallback(
    (
      modelId: string,
      viewportBounds: MapBounds,
      startUtc: string,
      endUtc: string,
      zoom?: number,
      terrain?: boolean,
    ) => {
      const start = floorHourIso(startUtc);
      const end = floorHourIso(endUtc);
      if (new Date(end).getTime() < new Date(start).getTime()) return;

      const entry = browseCacheRef.current[modelId];
      const gap = missingRange(entry?.series, start, end);
      if (!gap) return;

      const useTerrain = terrain ?? windNinjaOpt;
      const z = zoom ?? browseFetchZoom(useTerrain);
      const cached = browseCacheRef.current[modelId];
      const reqBounds = cached?.region ?? viewportBounds;
      const key = `${gap.start}|${gap.end}|${reqBounds.lat_min},${reqBounds.lon_min},${reqBounds.lat_max},${reqBounds.lon_max}|${z}|t=${useTerrain ? 1 : 0}`;
      if (prefetchKeyRef.current.get(modelId) === key) return;

      prefetchAbortRefs.current.get(modelId)?.abort();
      const ac = new AbortController();
      prefetchAbortRefs.current.set(modelId, ac);
      prefetchKeyRef.current.set(modelId, key);

      fetchWindArea(reqBounds, modelId, gap.start, gap.end, ac.signal, z, useTerrain)
        .then(({ series }) => {
          if (ac.signal.aborted || !series) return;
          const seriesTier = series.zoom_tier ?? tierForZoom(z);
          setBrowseCache((prev) => {
            const existing = prev[modelId];
            if (!existing) {
              return {
                [modelId]: {
                  series,
                  region: series.bbox ?? reqBounds,
                  loadedEndUtc: series.times[series.times.length - 1] ?? gap.end,
                  tier: seriesTier,
                },
              };
            }
            const merged = mergeSeries(existing.series, series);
            return {
              [modelId]: {
                series: merged,
                region: existing.region,
                loadedEndUtc: merged.times[merged.times.length - 1] ?? existing.loadedEndUtc,
                tier: existing.tier,
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
    [windNinjaOpt],
  );

  const scheduleBrowseWindow = useCallback(
    (
      modelId: string,
      viewportBounds: MapBounds,
      focusUtc: string,
      horizonEnd: string,
      opts?: { forceReplace?: boolean },
      zoom?: number,
      terrain?: boolean,
    ) => {
      const focus = floorHourIso(focusUtc);
      const entry = browseCacheRef.current[modelId];
      const useTerrain = terrain ?? windNinjaOpt;
      const z = zoom ?? browseFetchZoom(useTerrain);
      const inRegion = !!(entry && bboxContains(entry.region, viewportBounds, 0.05));
      const merge = inRegion && !opts?.forceReplace;
      // First fetch already covers focus..(focus+PREFETCH_WINDOW_H-1).
      const needFocus = !merge || !entry || !seriesCoversHour(entry.series, focus);

      const kickPrefetch = () => {
        // Extend beyond the initial window when scrubbing forward.
        const aheadEnd = capHour(hoursAheadIso(focus, PREFETCH_WINDOW_H - 1), horizonEnd);
        const prefetchStart = hoursAheadIso(focus, 1);
        if (new Date(prefetchStart).getTime() <= new Date(aheadEnd).getTime()) {
          prefetchBrowseRange(modelId, viewportBounds, prefetchStart, aheadEnd, z, useTerrain);
        }
      };

      if (needFocus) {
        if (!merge) {
          prefetchAbortRefs.current.get(modelId)?.abort();
          prefetchKeyRef.current.delete(modelId);
        }
        fetchBrowseFocus(
          modelId,
          merge && entry ? entry.region : viewportBounds,
          focus,
          merge,
          merge ? undefined : kickPrefetch,
          z,
          useTerrain,
        );
        if (merge) queueMicrotask(kickPrefetch);
      } else {
        kickPrefetch();
      }
    },
    [fetchBrowseFocus, prefetchBrowseRange, windNinjaOpt],
  );

  const applyHighDef = useCallback(
    (on: boolean) => {
      setWindNinjaOpt(on);
      if (inRouteReview) return;
      if (activeModelId) {
        browseAbortRefs.current.get(activeModelId)?.abort();
        prefetchAbortRefs.current.get(activeModelId)?.abort();
        prefetchKeyRef.current.delete(activeModelId);
        const next = { ...browseCacheRef.current };
        delete next[activeModelId];
        browseCacheRef.current = next;
        setBrowseCache(next);
        setStatus(activeModelId, "fetching");
      } else {
        browseCacheRef.current = {};
        setBrowseCache({});
      }
    },
    [
      inRouteReview,
      activeModelId,
      setStatus,
    ],
  );

  // Load route-window series into routeCache per route model (silent; no overlay).
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
      // Do not set overlayStatus fetching — would flash wind load modal in review.

      fetchWindArea(bbox, modelId, start, end, ac.signal, undefined, undefined, true)
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
    if (routes.length > 0 || routeModal !== "hidden") return;
    if (!viewport || apiUp !== true) return;
    if (!activeModelId || !selected.has(activeModelId)) return;

    const t = setTimeout(() => {
      const id = activeModelId;
      const focus = floorHourIso(overlayTimeUtc);
      const entry = browseCacheRef.current[id];
      const fetchZoom = browseFetchZoom(windNinjaOpt);
      const inRegion = !!(entry && bboxContains(entry.region, viewport, 0.05));
      const covers = !!(entry && seriesCoversHour(entry.series, focus));
      const skipFetch = inRegion && covers;
      if (skipFetch) {
        const aheadEnd = capHour(hoursAheadIso(focus, PREFETCH_WINDOW_H - 1), horizonEndUtc);
        const prefetchStart = hoursAheadIso(focus, 1);
        if (new Date(prefetchStart).getTime() <= new Date(aheadEnd).getTime()) {
          prefetchBrowseRange(id, viewport, prefetchStart, aheadEnd, fetchZoom);
        }
        return;
      }
      scheduleBrowseWindow(id, viewport, focus, horizonEndUtc, {
        forceReplace: !inRegion,
      }, fetchZoom);
    }, 250);

    return () => clearTimeout(t);
  }, [
    viewport,
    mapZoom,
    windNinjaOpt,
    selected,
    activeModelId,
    apiUp,
    routes.length,
    routeModal,
    overlayTimeUtc,
    horizonEndUtc,
    scheduleBrowseWindow,
    prefetchBrowseRange,
  ]);

  // Penultimate of contiguous run → prefetch next PREFETCH_WINDOW_H hours after runMax.
  useEffect(() => {
    if (routes.length > 0 || routeModal !== "hidden") return;
    if (!viewport || !activeModelId || apiUp !== true) return;
    const entry = browseCache[activeModelId];
    if (!entry) return;
    const inRegion = bboxContains(entry.region, viewport, 0.05);
    if (!inRegion) return;

    const focus = floorHourIso(overlayTimeUtc);
    const runMax = contiguousRunMax(entry.series, focus);
    if (!runMax) return;
    const penultimate = hoursAheadIso(runMax, -1);
    if (focus !== penultimate) return;

    const extendStart = hoursAheadIso(runMax, 1);
    const extendEnd = capHour(hoursAheadIso(runMax, PREFETCH_WINDOW_H), horizonEndUtc);
    if (new Date(extendStart).getTime() > new Date(extendEnd).getTime()) return;
    prefetchBrowseRange(
      activeModelId,
      viewport,
      extendStart,
      extendEnd,
      browseFetchZoom(windNinjaOpt),
    );
  }, [
    overlayTimeUtc,
    browseCache,
    viewport,
    windNinjaOpt,
    activeModelId,
    apiUp,
    routes.length,
    routeModal,
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

  const handleViewportChange = useCallback((bounds: MapBounds, _span: number, zoom: number) => {
    setViewport(bounds);
    setMapZoom(zoom);
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
        .catch((err) => {
          if (ac.signal.aborted) return;
          console.warn("[forecast] /models/available failed", {
            cell,
            error: err instanceof Error ? err.message : String(err),
          });
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
        marks: withSequentialMarkNames([
          ...c.marks,
          {
            position: [lat, lon],
            radius_nm: 0.3,
            rounding: "either",
          },
        ]),
      }));
    setEditMode("none");
  };

  const clearCourse = () => {
    setCourse((c) => ({
      ...c,
      start: [0, 0],
      finish: [0, 0],
      marks: [],
    }));
    setEditMode("none");
  };

  const copySummary = async () => {
    const text = formatSummaryClipboard(course, selected, availableModels);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* clipboard may be blocked */
    }
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

  const dismissRouteModal = () => {
    setRouteModal("hidden");
    setRouteSteps([]);
    setRouteWarnings([]);
    setRunMsg("");
    setPrepState("idle");
    setRoutes([]);
    setRouteCache({});
    setRetryableModelIds([]);
    pendingRoutesRef.current = [];
    routeRunModelIdsRef.current = [];
  };

  const finishRouteSuccess = (okRoutes: ModelRoute[], warnings: string[]) => {
    pendingRoutesRef.current = [];
    setRetryableModelIds([]);
    setRoutes(okRoutes);
    setActiveModelId(okRoutes[0].model_id);
    setRouteIndex(0);
    setOverlayTimeUtc(courseForRunRef.current.depart_utc);
    setHorizonStartUtc(courseForRunRef.current.depart_utc);
    setShowWind(true);
    setFitRouteKey(`${Date.now()}-${okRoutes.length}`);
    pinChartCache().catch(() => {
      /* The route is already in localStorage if the chart cache is unavailable. */
    });
    if (warnings.length > 0) {
      setPrepState("warning");
      setRunMsg(warnings[0]);
      setRouteWarnings(warnings);
    } else {
      setPrepState("ready");
      setRunMsg("");
      setRouteWarnings([]);
    }
    setRouteSteps((prev) =>
      prev.map((s) => {
        if (s.status === "done" || s.status === "error") return s;
        const ms =
          s.status === "active" && s.ms != null ? Math.max(0, Date.now() - s.ms) : s.ms;
        return { ...s, status: "done" as const, ms };
      }),
    );
    setRouteModal("done");
  };

  /** Dismiss the post-route summary overlay; keep routes for review. */
  const acknowledgeRouteResults = () => {
    setRouteModal("hidden");
    setRouteSteps([]);
  };

  /** Leave route review → browse / edit course. */
  const exitRouting = () => {
    for (const ac of routeAbortRefs.current.values()) ac.abort();
    routeAbortRefs.current.clear();
    dismissRouteModal();
    const now = currentForecastHourUtc();
    setOverlayTimeUtc(now);
    setHorizonStartUtc(now);
    setFitRouteKey("");
    setDetailOpen(false);
  };

  const runRoute = async (courseOverride?: Course) => {
    // onClick handlers pass a MouseEvent if wired as onClick={runRoute}; only a
    // real Course override (e.g. live reroute) may replace the editor course.
    const courseForRun =
      courseOverride &&
      Array.isArray(courseOverride.start) &&
      Array.isArray(courseOverride.finish)
        ? courseOverride
        : course;
    courseForRunRef.current = courseForRun;
    const modelIds = Array.from(selected);
    if (!boat.polar_id) {
      setPrepState("error");
      setRunMsg("Load a polar first.");
      return;
    }
    if (!courseIsReady(courseForRun)) {
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
    setRouteModal("running");
    setPrepState("preparing");
    setRunMsg("");
    setRouteWarnings([]);
    setRoutes([]);
    setRouteCache({});
    setRetryableModelIds([]);
    pendingRoutesRef.current = [];
    routeRunModelIdsRef.current = modelIds;
    setRouteSteps(initialRouteSteps(modelIds, courseForRun));
    try {
      const warningsAcc: string[] = [];
      const res = await api.routeStream(
        {
          course: courseForRun,
          boat,
          model_ids: modelIds,
          time_step_s: 300,
          heading_step_deg: 7.5,
        },
        (event) => {
          if (event.type === "stage" && event.status === "start") {
            setPrepState("ready");
          }
          if (event.type === "warning") {
            warningsAcc.push(event.message);
            setRouteWarnings([...warningsAcc]);
          } else if (event.type === "error") {
            warningsAcc.push(event.message);
            setRouteWarnings([...warningsAcc]);
            setPrepState("error");
            setRunMsg(event.message);
          }
          setRouteSteps((prev) => applyRouteEvent(prev, event));
        },
      );

      const allWarnings = [...new Set([...warningsAcc, ...res.warnings])];
      setRouteWarnings(allWarnings);

      const okRoutes = res.routes.filter((r) => !r.unreachable);
      const failedModelIds = modelIds.filter(
        (id) => !okRoutes.some((r) => r.model_id === id),
      );

      if (failedModelIds.length > 0) {
        // Hold successes for merge after per-card retry; no partial route on map yet.
        pendingRoutesRef.current = okRoutes;
        setRoutes([]);
        setRetryableModelIds(failedModelIds);
        setPrepState("error");
        setRunMsg(
          allWarnings.find(
            (w) =>
              !/forecast exhausted/i.test(w) &&
              (w.includes("unreachable") || w.includes("unfinished")),
          ) ||
            allWarnings.find((w) => !/forecast exhausted/i.test(w)) ||
            "Route unfinished — some marks unreachable",
        );
        setRouteSteps((prev) => {
          const copy = prev.map((s) => ({ ...s }));
          for (const mid of failedModelIds) {
            const belongs = (id: string) =>
              id === `wind:${mid}` ||
              id === `prepare:${mid}` ||
              id.startsWith(`leg:${mid}:`);
            const hasHardError = copy.some(
              (s) => belongs(s.id) && s.status === "error",
            );
            if (hasHardError) continue;
            // Forecast exhausted: leave the segment spinning — not an error.
            const exhaustedOnly = allWarnings.some(
              (w) => w.includes(mid) && /forecast exhausted/i.test(w),
            );
            if (exhaustedOnly) continue;
            const active = copy.findIndex(
              (s) => belongs(s.id) && s.status === "active",
            );
            const pendingLeg = copy.findIndex(
              (s) => s.id.startsWith(`leg:${mid}:`) && s.status === "pending",
            );
            const target = active >= 0 ? active : pendingLeg;
            if (target >= 0) {
              copy[target] = {
                ...copy[target],
                status: "error",
                detail: allWarnings[0] || "unreachable",
              };
            }
          }
          return copy;
        });
        setRouteModal("error");
      } else {
        finishRouteSuccess(okRoutes, allWarnings);
      }
    } catch (e) {
      console.error("[route] failed:", e);
      setPrepState("error");
      setRunMsg(String(e));
      setRoutes([]);
      setRetryableModelIds(modelIds);
      setRouteSteps((prev) => {
        const active = prev.findIndex((s) => s.status === "active");
        if (active < 0) return prev;
        const copy = prev.map((s) => ({ ...s }));
        copy[active] = { ...copy[active], status: "error", detail: String(e) };
        return copy;
      });
      setRouteModal("error");
    } finally {
      setRunning(false);
    }
  };

  /** Re-route a single failed model without re-running the whole workflow. */
  const retryModel = async (modelId: string) => {
    if (!boat.polar_id || !courseIsReady(course)) return;
    setRunning(true);
    setRouteModal("running");
    setPrepState("preparing");
    setRunMsg("");
    setRetryableModelIds((prev) => prev.filter((id) => id !== modelId));
    setRouteSteps((prev) => resetModelSteps(prev, modelId, course));
    try {
      const warningsAcc: string[] = [];
      const res = await api.routeStream(
        {
          course,
          boat,
          model_ids: [modelId],
          time_step_s: 300,
          heading_step_deg: 7.5,
        },
        (event) => {
          if (event.type === "stage" && event.status === "start") {
            setPrepState("ready");
          }
          if (event.type === "warning") {
            warningsAcc.push(event.message);
            setRouteWarnings((prev) => [...new Set([...prev, ...warningsAcc])]);
          } else if (event.type === "error") {
            warningsAcc.push(event.message);
            setRouteWarnings((prev) => [...new Set([...prev, ...warningsAcc])]);
            setPrepState("error");
            setRunMsg(event.message);
          }
          setRouteSteps((prev) => applyRouteEvent(prev, event));
        },
      );

      const allWarnings = [...new Set([...warningsAcc, ...res.warnings])];
      const ok = res.routes.filter((r) => !r.unreachable && r.model_id === modelId);
      if (ok.length === 0) {
        setRetryableModelIds((prev) =>
          prev.includes(modelId) ? prev : [...prev, modelId],
        );
        setPrepState("error");
        setRunMsg(
          allWarnings.find((w) => !/forecast exhausted/i.test(w)) ||
            `${modelId}: route unfinished`,
        );
        setRouteSteps((prev) => {
          const copy = prev.map((s) => ({ ...s }));
          const belongs = (id: string) =>
            id === `wind:${modelId}` ||
            id === `prepare:${modelId}` ||
            id.startsWith(`leg:${modelId}:`);
          const hasHardError = copy.some(
            (s) => belongs(s.id) && s.status === "error",
          );
          const exhaustedOnly = allWarnings.some(
            (w) => w.includes(modelId) && /forecast exhausted/i.test(w),
          );
          if (!hasHardError && !exhaustedOnly) {
            const active = copy.findIndex(
              (s) => belongs(s.id) && s.status === "active",
            );
            if (active >= 0) {
              copy[active] = {
                ...copy[active],
                status: "error",
                detail: allWarnings[0] || "unreachable",
              };
            }
          }
          return copy;
        });
        setRouteModal("error");
        return;
      }

      pendingRoutesRef.current = [
        ...pendingRoutesRef.current.filter((r) => r.model_id !== modelId),
        ...ok,
      ];
      const runIds = routeRunModelIdsRef.current;
      const stillFailed = runIds.filter(
        (id) => !pendingRoutesRef.current.some((r) => r.model_id === id),
      );
      if (stillFailed.length === 0) {
        finishRouteSuccess(pendingRoutesRef.current, allWarnings);
      } else {
        setRetryableModelIds(stillFailed);
        setRouteWarnings(allWarnings);
        setRouteModal("error");
        setPrepState("error");
      }
    } catch (e) {
      console.error("[route] model retry failed:", e);
      setPrepState("error");
      setRunMsg(String(e));
      setRetryableModelIds((prev) =>
        prev.includes(modelId) ? prev : [...prev, modelId],
      );
      setRouteSteps((prev) => {
        const copy = prev.map((s) => ({ ...s }));
        const active = copy.findIndex(
          (s) =>
            s.status === "active" &&
            (s.id === `wind:${modelId}` ||
              s.id === `prepare:${modelId}` ||
              s.id.startsWith(`leg:${modelId}:`)),
        );
        if (active >= 0) {
          copy[active] = { ...copy[active], status: "error", detail: String(e) };
        }
        return copy;
      });
      setRouteModal("error");
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
          <div className="rail-section-head">
            <h2>Course</h2>
            <button
              type="button"
              className="rail-section-action"
              onClick={clearCourse}
              disabled={!courseHasPoints}
              title="Clear course"
            >
              Clear
            </button>
          </div>
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
          <div className="rail-section-head">
            <h2>Summary</h2>
            <button
              type="button"
              className="rail-section-action"
              onClick={copySummary}
              disabled={!courseIsReady(course)}
              title="Copy summary to clipboard"
            >
              Copy
            </button>
          </div>
          <RouteReview
            course={course}
            selected={selected}
            models={availableModels}
            running={running || routeModal === "error"}
            prepState={prepState}
            message={routeModal === "hidden" ? runMsg || undefined : undefined}
            onRun={() => {
              void runRoute();
            }}
          />
        </div>
      </aside>

      <div className={`map-area${inRouteReview ? " map-area--review" : ""}`}>
        <MapView
          routes={routes}
          course={course}
          showWind={showWind}
          showDepth={showDepth}
          windTimeUtc={routes.length > 0 ? scrubHourUtc : overlayTimeUtc}
          windSeries={windSeries}
          playbackPosition={playbackPosition}
          playbackHeading={playbackHeading}
          livePosition={live && liveFix ? liveFix.position : null}
          liveHeading={liveFix?.headingDeg ?? null}
          planPosition={live && planNow ? planNow.position : null}
          planHeading={live && planNow ? planNow.heading : null}
          fitRouteKey={fitRouteKey}
          editMode={editMode}
          onMapClick={editMode === "none" ? undefined : handleMapClick}
          onRouteSelect={setActiveModelId}
          onRouteSeek={(id, t) => {
            setActiveModelId(id);
            setOverlayTimeUtc(t);
          }}
          onCoursePointMove={(kind, index, lat, lon) => {
            setCourse((c) => {
              if (kind === "start") return { ...c, start: [lat, lon] };
              if (kind === "finish") return { ...c, finish: [lat, lon] };
              if (kind === "mark" && index != null) {
                return {
                  ...c,
                  marks: c.marks.map((mk, i) =>
                    i === index ? { ...mk, position: [lat, lon] } : mk,
                  ),
                };
              }
              return c;
            });
          }}
          onOverlayError={setOverlayMsg}
          onViewportChange={handleViewportChange}
        />
        {inRouteReview && (
          <RouteModelTabs
            routes={routes}
            activeModelId={activeModelId}
            modelLabels={modelLabels}
            onActiveModel={setActiveModelId}
          />
        )}
        <div className="map-corner-stack">
        <OfflinePack course={course} canDownload={courseIsReady(course)} />
        <LiveRacePanel
          enabled={live}
          onToggle={() => {
            setLive((on) => {
              const next = !on;
              if (next) setLiveFollowNow(true);
              return next;
            });
          }}
          followNow={liveFollowNow}
          onFollowNow={() => {
            setLiveFollowNow(true);
            setNowUtc(new Date().toISOString());
          }}
          fix={liveFix}
          error={liveError}
          legs={activeRoute?.legs ?? null}
          departUtc={course.depart_utc}
          nowUtc={nowUtc}
          canReroute={Boolean(liveFix) && isPointSet(course.finish) && !running}
          onUseAsStart={() => {
            if (!liveFix) return;
            setCourse((current) => ({ ...current, start: liveFix.position }));
          }}
          onReroute={() => {
            if (!liveFix || !isPointSet(course.finish)) return;
            const next: Course = {
              ...course,
              start: liveFix.position,
              depart_utc: new Date().toISOString(),
            };
            setCourse(next);
            setLiveFollowNow(true);
            setNowUtc(next.depart_utc);
            void runRoute(next);
          }}
        />
        </div>
        <TimeScrubber
          overlayTimeUtc={overlayTimeUtc}
          onTime={(t) => {
            setLiveFollowNow(false);
            setOverlayTimeUtc(t);
          }}
          horizonStartUtc={horizonStartUtc}
          horizonEndUtc={inRouteReview ? routeHorizonEndUtc : horizonEndUtc}
          loading={windLoading && !inRouteReview}
          routeActive={inRouteReview}
          leg={playbackLeg}
          legs={activeRoute?.legs ?? null}
          playUntilUtc={activeRoute?.eta_utc ?? null}
          distanceNm={playbackDistanceNm}
        />
        <MapLoadOverlay
          kind={mapLoadKind}
          label={mapLoadLabel}
          routeSteps={routeLoading ? routeSteps : undefined}
          routeModelIds={routeLoading ? Array.from(selected) : undefined}
          routeModelLabels={modelLabels}
          windSteps={!routeLoading && mapLoadKind === "wind-initial" ? windSteps : undefined}
          routeWarnings={routeLoading ? routeWarnings : undefined}
          routeError={routeModal === "error"}
          retryableModelIds={retryableModelIds}
          completedRoutes={routeModal === "done" ? routes : undefined}
          onRetry={() => {
            void runRoute();
          }}
          onRetryModel={retryModel}
          onEdit={dismissRouteModal}
          onView={acknowledgeRouteResults}
        />
        {activeError && <div className="map-error-banner">{activeError}</div>}
        <MapControls
          toggles={{ showWind, showDepth }}
          highDef={windNinjaOpt}
          highDefDisabled={inRouteReview}
          onHighDef={applyHighDef}
          onChange={(patch) => {
            if (patch.showWind !== undefined) setShowWind(patch.showWind);
            if (patch.showDepth !== undefined) setShowDepth(patch.showDepth);
          }}
        />
        <ZoomDebugChip
          zoom={mapZoom}
          forecastZoom={browseFetchZoom(windNinjaOpt)}
          terrainOn={windNinjaOpt}
          terrainDisabled={inRouteReview}
          terrainLibrary={windSeries?.terrain_library ?? null}
          terrainApplied={windSeries?.terrain_applied ?? null}
          terrainLibraryVersion={windSeries?.terrain_library_version ?? null}
          onTerrainToggle={applyHighDef}
        />
        <WindLegend visible={showWind && !!windSeries} />
        {inRouteReview ? (
          <div className="forecast-dock">
            <button
              type="button"
              className="exit-routing-btn"
              onClick={exitRouting}
            >
              Exit routing
            </button>
          </div>
        ) : (
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
              apiUp={apiUp}
              hasViewport={viewport != null}
            />
          </div>
        )}
      </div>

      <RouteDetailPanel
        routes={routes}
        routeIndex={displayRouteIndex}
        currentTimeUtc={overlayTimeUtc}
        open={detailOpen}
        onToggle={() => setDetailOpen((o) => !o)}
        onRouteIndex={(i) => {
          setRouteIndex(i);
          if (routes[i]) setActiveModelId(routes[i].model_id);
        }}
        onSeek={setOverlayTimeUtc}
        modelLabels={Object.fromEntries(availableModels.map((m) => [m.id, m.label]))}
      />
    </div>
  );
}
