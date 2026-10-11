"use client";

import dynamic from "next/dynamic";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BoatConfigPanel from "@/components/BoatConfigPanel";
import CourseEditor, {
  courseCollapsedSummary,
  type EditMode,
  type StartSource,
} from "@/components/CourseEditor";
import { useLivePosition } from "@/components/LiveRacePanel";
import InstrumentCluster from "@/components/InstrumentCluster";
import { type ModelStatus } from "@/components/RouteBuildProgress";
import RouteProgress, {
  applyRouteEvent,
  initialRouteSteps,
  resetModelSteps,
} from "@/components/RouteProgress";
import RouteLegTable from "@/components/RouteLegTable";
import RouteReview from "@/components/RouteReview";
import WindLegend from "@/components/WindLegend";
import BrowseTimeDock from "@/components/shell/BrowseTimeDock";
import ForecastCheckList from "@/components/shell/ForecastCheckList";
import MapIconControls from "@/components/shell/MapIconControls";
import { api } from "@/lib/api";
import { pinChartCache } from "@/lib/deviceCache";
import { downloadVoyageChart } from "@/lib/voyagePack";
import {
  boundsFromCourseAndRoutes,
  clearStoredRouteSession,
  courseIsReady,
  courseWithEffectiveFinish,
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
  floorHourIso,
  hoursAheadIso,
  mergeSeries,
  missingRange,
  seriesCoversHour,
  type WindSeries,
} from "@/lib/meteoGrid";
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
  /** Terrain toggle used when this series was fetched. */
  terrain: boolean;
};

function courseBBox(course: Course, margin = SERIES_MARGIN_DEG): MapBounds | null {
  const box = boundsFromCourseAndRoutes(course);
  if (!box) return null;
  return {
    lat_min: box.lat_min - margin,
    lat_max: box.lat_max + margin,
    lon_min: box.lon_min - margin,
    lon_max: box.lon_max + margin,
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

export default function PlannerWorkspace() {
  const pathname = usePathname();
  const router = useRouter();
  const mode: "basic" | "routing" =
    pathname === "/route" || pathname?.startsWith("/route/") ? "routing" : "basic";
  const modeRef = useRef(mode);
  modeRef.current = mode;

  const RAIL_MIN = 320;
  const RAIL_MAX = 720;
  const RAIL_DEFAULT = 360;
  const RAIL_WIDTH_KEY = "sailing-router-rail-width-v1";
  const [railWidth, setRailWidth] = useState(RAIL_DEFAULT);
  const railWidthRef = useRef(railWidth);
  railWidthRef.current = railWidth;
  const railDragRef = useRef<{ startX: number; startW: number } | null>(null);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(RAIL_WIDTH_KEY);
      const n = raw ? Number(raw) : NaN;
      if (Number.isFinite(n)) {
        setRailWidth(Math.min(RAIL_MAX, Math.max(RAIL_MIN, n)));
      }
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const drag = railDragRef.current;
      if (!drag) return;
      const next = Math.min(
        RAIL_MAX,
        Math.max(RAIL_MIN, drag.startW + (e.clientX - drag.startX)),
      );
      railWidthRef.current = next;
      setRailWidth(next);
    };
    const onUp = () => {
      if (!railDragRef.current) return;
      railDragRef.current = null;
      try {
        localStorage.setItem(RAIL_WIDTH_KEY, String(railWidthRef.current));
      } catch {
        /* ignore */
      }
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, []);

  const [apiUp, setApiUp] = useState<boolean | null>(null);
  const [availableModels, setAvailableModels] = useState<ModelPreset[]>([]);
  const [boat, setBoat] = useState<BoatConfig>(INITIAL_BOAT);
  const [course, setCourse] = useState<Course>(INITIAL_COURSE);
  const [courseReadyToPersist, setCourseReadyToPersist] = useState(false);
  const [sessionHydrated, setSessionHydrated] = useState(false);
  /** Opaque cover until hydrate + first map frame (avoids sea-view jump). */
  const [mapBooted, setMapBooted] = useState(false);
  const skipNextRouteClear = useRef(true);
  const courseForRunRef = useRef(course);
  const [live, setLive] = useState(false);
  const [liveFollowNow, setLiveFollowNow] = useState(true);
  const [nowUtc, setNowUtc] = useState(() => new Date().toISOString());
  const { fix: liveFix, error: liveError } = useLivePosition(live);
  const [editMode, setEditMode] = useState<EditMode>("none");
  const [startSource, setStartSource] = useState<StartSource>("point");
  const [setupOpen, setSetupOpen] = useState({
    course: true,
    boat: false,
    forecasts: false,
  });
  /** Open one section at a time; clicking the open one collapses all. */
  const toggleSetup = (key: keyof typeof setupOpen) => {
    setSetupOpen((prev) => {
      if (prev[key]) {
        return { course: false, boat: false, forecasts: false };
      }
      return { course: false, boat: false, forecasts: false, [key]: true };
    });
  };
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string>("");
  const [overlayMsg, setOverlayMsg] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [showWind, setShowWind] = useState(true);
  const [showDepth, setShowDepth] = useState(true);
  const [viewport, setViewport] = useState<MapBounds | null>(null);
  const [mapZoom, setMapZoom] = useState(5);
  /** WindNinja terrain downscaling — browse overlay and /route isochrone. */
  const [windNinjaOpt, setWindNinjaOpt] = useState(false);

  const [activeModelId, setActiveModelId] = useState("");
  const [routeIndex, setRouteIndex] = useState(0);
  const [fitRouteKey, setFitRouteKey] = useState("");
  /** Frame the route once per course session when entering route mode; mode toggles keep the camera. */
  const framedRouteKeyRef = useRef<string | null>(null);
  const [prepState, setPrepState] = useState<
    "idle" | "preparing" | "ready" | "error" | "warning"
  >("idle");
  const [routeModal, setRouteModal] = useState<"hidden" | "running" | "error" | "done">("hidden");
  const [routeSteps, setRouteSteps] = useState<RouteProgressStep[]>([]);
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
    if (mode === "routing" && routes.length > 0) return;
    setBrowseCache((prev) => {
      if (!activeModelId) return {};
      const keep = prev[activeModelId];
      if (!keep) return {};
      if (Object.keys(prev).length === 1 && prev[activeModelId]) return prev;
      return { [activeModelId]: keep };
    });
  }, [activeModelId, mode, routes.length]);

  const horizonEndUtc = useMemo(
    () => hoursAheadIso(horizonStartUtc, SCRUB_HORIZON_H),
    [horizonStartUtc],
  );

  const activeRouteIndex = routes.findIndex((r) => r.model_id === activeModelId);
  const displayRouteIndex = activeRouteIndex >= 0 ? activeRouteIndex : routeIndex;
  const activeRoute = mode === "routing" ? routes[displayRouteIndex] : undefined;
  // Home always uses viewport browse wind. Route review uses the course-window
  // cache; route setup (no result yet) still browses like home.
  const windSeries =
    mode === "routing" && routes.length > 0
      ? (activeModelId && routeCache[activeModelId]) || null
      : (activeModelId && browseCache[activeModelId]?.series) || null;

  const scrubHourUtc = floorHourIso(overlayTimeUtc);
  const activeStatus = activeModelId ? overlayStatus[activeModelId] : undefined;
  const inRouteReview = mode === "routing" && routes.length > 0;
  const mapMode: "browse" | "routeReview" =
    mode === "routing" &&
    (routes.length > 0 || routeModal === "running" || routeModal === "error")
      ? "routeReview"
      : "browse";
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
    if (!live || !liveFix || startSource !== "location") return;
    setCourse((current) => ({ ...current, start: liveFix.position }));
  }, [live, liveFix, startSource]);

  // Basic browse: ensure at least one forecast is selected so wind loads without a dock picker.
  useEffect(() => {
    if (availableModels.length === 0) return;
    setSelected((prev) => {
      if (prev.size > 0) {
        const stillValid = [...prev].filter((id) => availableModels.some((m) => m.id === id));
        if (stillValid.length === prev.size) return prev;
        if (stillValid.length > 0) return new Set(stillValid);
      }
      return new Set([availableModels[0].id]);
    });
    setActiveModelId((id) => {
      if (id && availableModels.some((m) => m.id === id)) return id;
      return availableModels[0].id;
    });
  }, [availableModels]);

  const playbackPosition = boatAtTime?.position ?? null;
  const playbackHeading = boatAtTime?.heading ?? null;
  const playbackLeg = boatAtTime?.leg ?? null;
  const playbackDistanceNm = useMemo(() => {
    if (!activeRoute?.legs?.length || !boatAtTime) return 0;
    return cumulativeDistanceNm(activeRoute.legs, boatAtTime.index, boatAtTime.position);
  }, [activeRoute, boatAtTime]);

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
    const hasRoutes =
      !!(session && session.courseKey === key && session.routes.length > 0);
    if (hasRoutes && session) {
      setRoutes(session.routes);
      setRouteWarnings(session.warnings ?? []);
      setActiveModelId(session.ui.activeModelId || session.routes[0].model_id);
      setRouteIndex(session.ui.routeIndex ?? 0);
      setOverlayTimeUtc(session.ui.overlayTimeUtc || nextCourse.depart_utc);
      setHorizonStartUtc(session.ui.horizonStartUtc || nextCourse.depart_utc);
      setPrepState("ready");
      skipNextRouteClear.current = true;
      // Stay on whatever path the user opened — do not force /route.
    } else {
      setOverlayTimeUtc(now);
      setHorizonStartUtc(now);
      if (session) clearStoredRouteSession();
      skipNextRouteClear.current = true;
    }
    setCourseReadyToPersist(true);
    setSessionHydrated(true);

    const hasCoursePts =
      isPointSet(nextCourse.start) ||
      isPointSet(nextCourse.finish) ||
      nextCourse.marks.some((m) => isPointSet(m.position));
    const onRouting =
      typeof window !== "undefined" &&
      (window.location.pathname === "/route" ||
        window.location.pathname.startsWith("/route/"));
    // Cold-start framing only. Route-mode recenter for saved routes is handled
    // once when entering routing; home keeps a course-only frame if needed.
    if (hasRoutes && onRouting) {
      framedRouteKeyRef.current = key;
      setFitRouteKey(`load-${Date.now()}`);
    } else if (hasCoursePts && !hasRoutes) {
      setFitRouteKey(`load-${Date.now()}`);
    }
  }, []);

  useEffect(() => {
    if (mapBooted) return;
    const t = window.setTimeout(() => setMapBooted(true), 5000);
    return () => window.clearTimeout(t);
  }, [mapBooted]);

  useEffect(() => {
    if (!courseReadyToPersist) return;
    saveStoredCourse(course);
  }, [course, courseReadyToPersist]);

  const mapBootBounds = useMemo(
    () =>
      sessionHydrated
        ? boundsFromCourseAndRoutes(course, mode === "routing" ? routes : [])
        : null,
    [sessionHydrated, course, routes, mode],
  );

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
    framedRouteKeyRef.current = null;
    setPrepState("idle");
    setRunMsg("");
    setRouteSteps([]);
    setRouteWarnings([]);
    setRouteModal("hidden");
  }, [courseRouteKey, sessionHydrated]);

  // First entry into route mode with a stored/computed route: frame once.
  // Later home ↔ route toggles keep the same camera.
  useEffect(() => {
    if (!sessionHydrated || mode !== "routing" || routes.length === 0) return;
    if (framedRouteKeyRef.current === courseRouteKey) return;
    framedRouteKeyRef.current = courseRouteKey;
    setFitRouteKey(`frame-${Date.now()}`);
  }, [sessionHydrated, mode, routes.length, courseRouteKey]);

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

      fetchWindArea(reqBounds, modelId, hour, endHour, ac.signal, z, useTerrain)
        .then(({ series, error }) => {
          if (ac.signal.aborted) return;
          if (series) {
            const seriesTier = series.zoom_tier ?? tierForZoom(z);
            setBrowseCache((prev) => {
              const existing = prev[modelId];
              const sameTerrain = existing?.terrain === useTerrain;
              const merged =
                merge && existing && sameTerrain
                  ? mergeSeries(existing.series, series)
                  : series;
              const last = merged.times[merged.times.length - 1] ?? hour;
              // Cache region = returned series bbox (what we can render).
              const cacheRegion = merged.bbox ?? series.bbox ?? reqBounds;
              return {
                [modelId]: {
                  series: merged,
                  region: merge && existing && sameTerrain ? existing.region : cacheRegion,
                  loadedEndUtc: last,
                  tier: merge && existing && sameTerrain ? existing.tier : seriesTier,
                  terrain: useTerrain,
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
            if (!existing || existing.terrain !== useTerrain) {
              return {
                [modelId]: {
                  series,
                  region: series.bbox ?? reqBounds,
                  loadedEndUtc: series.times[series.times.length - 1] ?? gap.end,
                  tier: seriesTier,
                  terrain: useTerrain,
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
                terrain: useTerrain,
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
      const terrainOk = entry?.terrain === useTerrain;
      const inRegion = !!(
        entry &&
        terrainOk &&
        bboxContains(entry.region, viewportBounds, 0.05)
      );
      const merge = inRegion && !opts?.forceReplace;
      // First fetch already covers focus..(focus+PREFETCH_WINDOW_H-1).
      const needFocus =
        !merge || !entry || !terrainOk || !seriesCoversHour(entry.series, focus);

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

  const applyTerrain = useCallback(
    (on: boolean) => {
      setWindNinjaOpt(on);
      // Route-review heatmaps + any browse cache must not keep the other mode.
      setRouteCache({});
      browseAbortRefs.current.forEach((ac) => ac.abort());
      prefetchAbortRefs.current.forEach((ac) => ac.abort());
      browseAbortRefs.current.clear();
      prefetchAbortRefs.current.clear();
      prefetchKeyRef.current.clear();
      browseCacheRef.current = {};
      setBrowseCache({});

      if (mapMode === "routeReview") {
        // Isochrone already sailed; overlay refetch waits until Edit/rerun.
        return;
      }
      if (!activeModelId || !viewport || apiUp !== true) return;
      setStatus(activeModelId, "fetching");
      scheduleBrowseWindow(
        activeModelId,
        viewport,
        overlayTimeUtc,
        horizonEndUtc,
        { forceReplace: true },
        browseFetchZoom(on),
        on,
      );
    },
    [
      mapMode,
      activeModelId,
      viewport,
      apiUp,
      overlayTimeUtc,
      horizonEndUtc,
      scheduleBrowseWindow,
      setStatus,
    ],
  );

  // Load route-window series into routeCache per route model (silent; no overlay).
  // Match the Terrain toggle used when the isochrone was sailed.
  useEffect(() => {
    if (routes.length === 0 || !courseIsReady(course) || apiUp !== true) return;

    const bbox = courseBBox(course);
    if (!bbox) return;
    for (const route of routes) {
      if (!route.legs.length) continue;
      const modelId = route.model_id;
      if (routeCache[modelId]) continue;

      routeAbortRefs.current.get(modelId)?.abort();
      const ac = new AbortController();
      routeAbortRefs.current.set(modelId, ac);
      const { start, end } = routeTimeWindow(route, course.depart_utc);
      // Do not set overlayStatus fetching — would flash wind load modal in review.
      const z = browseFetchZoom(windNinjaOpt);

      fetchWindArea(bbox, modelId, start, end, ac.signal, z, windNinjaOpt, true)
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
  }, [routes, courseRouteKey, course, apiUp, setStatus, windNinjaOpt]);

  // Browse: only the active model — wind series stay small in RAM; chart tiles are on disk.
  // Home always browses. Route mode skips only while a route result owns the overlay.
  useEffect(() => {
    if (mapMode === "routeReview") return;
    if (!viewport || apiUp !== true) return;
    if (!activeModelId || !selected.has(activeModelId)) return;

    const t = setTimeout(() => {
      const id = activeModelId;
      const focus = floorHourIso(overlayTimeUtc);
      const entry = browseCacheRef.current[id];
      const fetchZoom = browseFetchZoom(windNinjaOpt);
      const terrainOk = entry?.terrain === windNinjaOpt;
      const inRegion = !!(
        entry &&
        terrainOk &&
        bboxContains(entry.region, viewport, 0.05)
      );
      const covers = !!(entry && seriesCoversHour(entry.series, focus));
      const skipFetch = inRegion && covers;
      if (skipFetch) {
        const aheadEnd = capHour(hoursAheadIso(focus, PREFETCH_WINDOW_H - 1), horizonEndUtc);
        const prefetchStart = hoursAheadIso(focus, 1);
        if (new Date(prefetchStart).getTime() <= new Date(aheadEnd).getTime()) {
          prefetchBrowseRange(id, viewport, prefetchStart, aheadEnd, fetchZoom, windNinjaOpt);
        }
        return;
      }
      scheduleBrowseWindow(id, viewport, focus, horizonEndUtc, {
        forceReplace: !inRegion || !terrainOk,
      }, fetchZoom, windNinjaOpt);
    }, 250);

    return () => clearTimeout(t);
  }, [
    viewport,
    mapZoom,
    windNinjaOpt,
    selected,
    activeModelId,
    apiUp,
    mapMode,
    overlayTimeUtc,
    horizonEndUtc,
    scheduleBrowseWindow,
    prefetchBrowseRange,
  ]);

  // Penultimate of contiguous run → prefetch next PREFETCH_WINDOW_H hours after runMax.
  useEffect(() => {
    if (mapMode === "routeReview") return;
    if (!viewport || !activeModelId || apiUp !== true) return;
    const entry = browseCache[activeModelId];
    if (!entry || entry.terrain !== windNinjaOpt) return;
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
      windNinjaOpt,
    );
  }, [
    overlayTimeUtc,
    browseCache,
    viewport,
    windNinjaOpt,
    activeModelId,
    apiUp,
    mapMode,
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
    else if (editMode === "mark") {
      setCourse((c) => {
        const pendingIdx = c.marks.findIndex((m) => !isPointSet(m.position));
        if (pendingIdx >= 0) {
          return {
            ...c,
            marks: c.marks.map((mk, i) =>
              i === pendingIdx ? { ...mk, position: [lat, lon] } : mk,
            ),
          };
        }
        return {
          ...c,
          marks: withSequentialMarkNames([
            ...c.marks,
            {
              position: [lat, lon],
              radius_nm: 0.05,
              rounding: "port",
              name: null,
            },
          ]),
        };
      });
    }
    setEditMode("none");
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
      if (viewport && apiUp === true && mapMode === "browse") {
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
    framedRouteKeyRef.current = null;
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
    // Frame in route mode only. If the user is on home, defer until they open
    // the planner so home ↔ route toggles do not yank the camera.
    if (modeRef.current === "routing") {
      framedRouteKeyRef.current = courseRouteKey;
      setFitRouteKey(`${Date.now()}-${okRoutes.length}`);
    } else {
      framedRouteKeyRef.current = null;
    }
    pinChartCache().catch(() => {
      /* The route is already in localStorage if the chart cache is unavailable. */
    });
    // Always cache voyage charts in the background after a successful route.
    downloadVoyageChart(window.location.origin, courseForRunRef.current, () => {
      /* progress unused — silent background save */
    }).catch(() => {
      /* Offline pack is best-effort. */
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
    // Drop loading UI — outcome appears in the rail review panel.
    setRouteSteps([]);
    setRouteModal("hidden");
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
  };

  const runRoute = async (courseOverride?: Course) => {
    // onClick handlers pass a MouseEvent if wired as onClick={runRoute}; only a
    // real Course override (e.g. live reroute) may replace the editor course.
    const rawCourse =
      courseOverride &&
      Array.isArray(courseOverride.start) &&
      Array.isArray(courseOverride.finish)
        ? courseOverride
        : course;
    const courseForRun = courseWithEffectiveFinish(rawCourse);
    courseForRunRef.current = courseForRun;
    const modelIds = Array.from(selected);
    if (!boat.polar_id) {
      setPrepState("error");
      setRunMsg("Load a polar first.");
      return;
    }
    if (!courseIsReady(rawCourse)) {
      setPrepState("error");
      setRunMsg("Add a start and at least one more point first.");
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
          terrain: windNinjaOpt,
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
          terrain: windNinjaOpt,
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

  const phase: "setup" | "running" | "review" =
    mode === "routing" && routes.length > 0
      ? "review"
      : mode === "routing" && (routeModal === "running" || routeModal === "error")
        ? "running"
        : "setup";

  const handleRerouteFromGps = () => {
    if (!liveFix || !isPointSet(course.finish)) return;
    setStartSource("location");
    const next: Course = {
      ...course,
      start: liveFix.position,
      depart_utc: new Date().toISOString(),
    };
    setCourse(next);
    setLiveFollowNow(true);
    setNowUtc(next.depart_utc);
    void runRoute(next);
  };

  const handleUseGpsStart = () => {
    setLive(true);
    setLiveFollowNow(true);
    setStartSource("location");
    if (liveFix) {
      setCourse((c) => ({ ...c, start: liveFix.position }));
    }
  };

  const goHome = () => {
    // Keep any computed route — only leave the routing chrome. Camera stays put.
    router.push("/");
  };

  const runDisabledReason =
    startSource === "location" && !live
      ? "Turn on GPS (top right) to start from current location"
      : startSource === "location" && !liveFix
        ? "Waiting for GPS fix…"
        : undefined;

  void liveError;
  void nowUtc;

  return (
    <div
      id="app-shell"
      className={mode === "basic" ? "app-shell--basic" : "app-shell--routing"}
      style={
        mode === "routing"
          ? ({ ["--rail-w" as string]: `${railWidth}px` } as React.CSSProperties)
          : undefined
      }
    >
      {mode === "routing" && (
        <aside className="rail">
          <div
            className="rail-resizer"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize planner panel"
            title="Drag to resize"
            onPointerDown={(e) => {
              e.preventDefault();
              railDragRef.current = { startX: e.clientX, startW: railWidth };
              document.body.style.cursor = "col-resize";
              document.body.style.userSelect = "none";
            }}
          />
          <div className="rail-topbar">
            <button
              type="button"
              className="rail-home-btn"
              onClick={goHome}
              title="Back to map"
              aria-label="Home"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path
                  d="M4 11.5L12 4l8 7.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1v-8.5z"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
            <span className="rail-topbar-title">Route planner</span>
          </div>

          {phase === "running" ? (
            <div className="rail-section rail-route-progress">
              <h2>{routeModal === "error" ? "Routing failed" : "Building routes"}</h2>
              <RouteProgress
                steps={routeSteps}
                modelIds={
                  routeRunModelIdsRef.current.length > 0
                    ? routeRunModelIdsRef.current
                    : Array.from(selected)
                }
                modelLabels={modelLabels}
                warnings={routeWarnings}
                showActions={routeModal === "error"}
                retryableModelIds={retryableModelIds}
                onRetry={
                  routeModal === "error"
                    ? () => {
                        void runRoute();
                      }
                    : undefined
                }
                onRetryModel={routeModal === "error" ? retryModel : undefined}
                onEdit={routeModal === "error" ? dismissRouteModal : undefined}
              />
            </div>
          ) : phase === "review" ? (
            <div className="rail-outcome">
              <div className="rail-vert-tabs" role="tablist" aria-label="Route forecast">
                {routes.map((r) => (
                  <button
                    key={r.model_id}
                    type="button"
                    role="tab"
                    aria-selected={r.model_id === activeModelId}
                    className={`rail-vert-tab${r.model_id === activeModelId ? " active" : ""}`}
                    onClick={() => setActiveModelId(r.model_id)}
                  >
                    {modelLabels[r.model_id] ?? r.model_id}
                  </button>
                ))}
              </div>
              <div className="rail-outcome-body">
                <div className="rail-section rail-section--summary">
                  <h2>Summary</h2>
                  {activeRoute && (
                    <dl className="rail-stat-grid">
                      <div>
                        <dt>Distance</dt>
                        <dd>{activeRoute.distance_nm.toFixed(1)} nm</dd>
                      </div>
                      <div>
                        <dt>ETA</dt>
                        <dd>
                          {activeRoute.eta_utc
                            ? new Date(activeRoute.eta_utc).toUTCString().slice(17, 22) + "Z"
                            : "—"}
                        </dd>
                      </div>
                      <div>
                        <dt>Legs</dt>
                        <dd>{activeRoute.legs.length}</dd>
                      </div>
                      <div>
                        <dt>Tacks</dt>
                        <dd>{activeRoute.n_tacks}</dd>
                      </div>
                    </dl>
                  )}
                </div>

                {playbackLeg && (
                  <div className="rail-section rail-section--instruments">
                    <InstrumentCluster
                      leg={playbackLeg}
                      distanceNm={playbackDistanceNm}
                      variant="rail"
                    />
                  </div>
                )}

                <div className="rail-section rail-section--legs">
                  <RouteLegTable
                    route={activeRoute}
                    currentTimeUtc={overlayTimeUtc}
                    onSeek={setOverlayTimeUtc}
                  />
                </div>

                <div className="rail-outcome-actions">
                  <button
                    type="button"
                    className="rail-reroute-btn"
                    onClick={exitRouting}
                  >
                    Edit route
                  </button>
                  <button
                    type="button"
                    className="rail-reroute-btn"
                    disabled={!live || !liveFix || running}
                    title={
                      live && liveFix
                        ? "Rebuild route from your current GPS position"
                        : "Turn on GPS (top right) to reroute from your location"
                    }
                    onClick={handleRerouteFromGps}
                  >
                    Reroute from current location
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <>
              <div
                className={`rail-section rail-section--bare${
                  setupOpen.course ? "" : " rail-section--collapsed"
                }`}
              >
                <button
                  type="button"
                  className="rail-accordion-head"
                  onClick={() => toggleSetup("course")}
                  aria-expanded={setupOpen.course}
                >
                  <div className="rail-accordion-titles">
                    <h2>Course</h2>
                    {!setupOpen.course && (
                      <span className="rail-accordion-summary">
                        {courseCollapsedSummary(course)}
                      </span>
                    )}
                  </div>
                  <span className="rail-accordion-chevron" aria-hidden>
                    {setupOpen.course ? "▾" : "▸"}
                  </span>
                </button>
                <div className="rail-accordion-body">
                  <CourseEditor
                    course={course}
                    onCourseChange={setCourse}
                    editMode={editMode}
                    onEditMode={setEditMode}
                    courseReady={courseIsReady(course)}
                    startSource={startSource}
                    onStartSource={setStartSource}
                    gpsAvailable={live && !!liveFix}
                    onUseGpsStart={handleUseGpsStart}
                  />
                </div>
              </div>

              <div
                className={`rail-section rail-section--bare${
                  setupOpen.boat ? "" : " rail-section--collapsed"
                }`}
              >
                <button
                  type="button"
                  className="rail-accordion-head"
                  onClick={() => toggleSetup("boat")}
                  aria-expanded={setupOpen.boat}
                >
                  <div className="rail-accordion-titles">
                    <h2>Boat</h2>
                    {!setupOpen.boat && (
                      <span className="rail-accordion-summary">
                        {boat.polar_id ? `Polar · ${boat.polar_id}` : "No polar loaded"}
                      </span>
                    )}
                  </div>
                  <span className="rail-accordion-chevron" aria-hidden>
                    {setupOpen.boat ? "▾" : "▸"}
                  </span>
                </button>
                <div className="rail-accordion-body">
                  <BoatConfigPanel
                    config={boat}
                    onConfigChange={setBoat}
                    minDepthM={course.min_depth_m}
                    onMinDepthChange={(m) => setCourse((c) => ({ ...c, min_depth_m: m }))}
                  />
                </div>
              </div>

              <div
                className={`rail-section rail-section--bare${
                  setupOpen.forecasts ? "" : " rail-section--collapsed"
                }`}
              >
                <button
                  type="button"
                  className="rail-accordion-head"
                  onClick={() => toggleSetup("forecasts")}
                  aria-expanded={setupOpen.forecasts}
                >
                  <div className="rail-accordion-titles">
                    <h2>Forecasts to run</h2>
                    {!setupOpen.forecasts && (
                      <span className="rail-accordion-summary">
                        {selected.size === 0
                          ? "None selected"
                          : `${selected.size} selected`}
                      </span>
                    )}
                  </div>
                  <span className="rail-accordion-chevron" aria-hidden>
                    {setupOpen.forecasts ? "▾" : "▸"}
                  </span>
                </button>
                <div className="rail-accordion-body">
                  <ForecastCheckList
                    models={availableModels}
                    selected={selected}
                    onToggle={toggleModel}
                    apiUp={apiUp}
                    hasViewport={viewport != null}
                  />
                </div>
              </div>

              <div className="rail-section">
                <div className="rail-section-head">
                  <h2>Summary</h2>
                </div>
                <RouteReview
                  course={course}
                  selected={selected}
                  models={availableModels}
                  running={running || routeModal === "error"}
                  prepState={prepState}
                  message={routeModal === "hidden" ? runMsg || undefined : undefined}
                  runDisabledReason={runDisabledReason}
                  onRun={() => {
                    void runRoute();
                  }}
                />
              </div>
            </>
          )}
        </aside>
      )}

      <div className={`map-area${inRouteReview ? " map-area--review" : ""}`}>
        {sessionHydrated && (
          <MapView
            routes={mode === "routing" ? routes : []}
            course={mode === "routing" ? course : null}
            showWind={showWind}
            showDepth={showDepth}
            windTimeUtc={inRouteReview ? scrubHourUtc : overlayTimeUtc}
            windSeries={windSeries}
            playbackPosition={mode === "routing" ? playbackPosition : null}
            playbackHeading={mode === "routing" ? playbackHeading : null}
            livePosition={live && liveFix ? liveFix.position : null}
            liveHeading={liveFix?.headingDeg ?? null}
            planPosition={live && planNow && mode === "routing" ? planNow.position : null}
            planHeading={live && planNow && mode === "routing" ? planNow.heading : null}
            fitRouteKey={fitRouteKey}
            initialBounds={mapBootBounds}
            editMode={mode === "routing" ? editMode : "none"}
            onMapClick={
              mode === "routing" && editMode !== "none" && startSource === "point"
                ? handleMapClick
                : mode === "routing" && editMode !== "none" && editMode !== "start"
                  ? handleMapClick
                  : undefined
            }
            onRouteSelect={setActiveModelId}
            onRouteSeek={(id, t) => {
              setActiveModelId(id);
              setOverlayTimeUtc(t);
            }}
            onCoursePointMove={(kind, index, lat, lon) => {
              if (kind === "start" && startSource === "location") return;
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
            onBootReady={() => setMapBooted(true)}
          />
        )}

        {mapBooted && (
          <>
            <div className="map-chrome-top-left">
              <BrowseTimeDock
                showModels={mode === "basic"}
                models={availableModels}
                selectedModels={selected}
                activeModelId={activeModelId}
                onActiveModel={setActiveModelId}
                onSelectModel={(id) => {
                  if (mode === "basic") {
                    setSelected(new Set([id]));
                    setActiveModelId(id);
                    return;
                  }
                  if (!selected.has(id)) toggleModel(id);
                }}
                overlayStatus={overlayStatus}
                overlayTimeUtc={overlayTimeUtc}
                onTime={(t) => {
                  setLiveFollowNow(false);
                  setOverlayTimeUtc(t);
                }}
                horizonStartUtc={horizonStartUtc}
                horizonEndUtc={inRouteReview ? routeHorizonEndUtc : horizonEndUtc}
                loading={windLoading && !inRouteReview}
                routeActive={inRouteReview}
                playUntilUtc={activeRoute?.eta_utc ?? null}
                legs={activeRoute?.legs ?? null}
                distanceNm={playbackDistanceNm}
              />
            </div>

            {mode === "basic" && (
              <div className="map-chrome-top-center">
                <button
                  type="button"
                  className="map-planner-route-btn"
                  onClick={() => router.push("/route")}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
                    <circle cx="12" cy="12" r="8.5" stroke="currentColor" strokeWidth="1.6" />
                    <path
                      d="M12 4.5v2.2M12 17.3v2.2M4.5 12h2.2M17.3 12h2.2"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                    />
                    <path d="M12 7.2l2.4 4.8-2.4 1.2-2.4-1.2L12 7.2z" fill="currentColor" />
                    <path
                      d="M12 16.8l-2.4-4.8 2.4-1.2 2.4 1.2-2.4 4.8z"
                      fill="currentColor"
                      opacity="0.35"
                    />
                  </svg>
                  Route planner
                </button>
              </div>
            )}

            {activeError && <div className="map-error-banner">{activeError}</div>}
            <MapIconControls
              toggles={{ showWind, showDepth }}
              terrain={windNinjaOpt}
              terrainDisabled={false}
              onTerrain={applyTerrain}
              gpsOn={live}
              onGpsChange={(on) => {
                setLive(on);
                if (on) setLiveFollowNow(true);
              }}
              onChange={(patch) => {
                if (patch.showWind !== undefined) setShowWind(patch.showWind);
                if (patch.showDepth !== undefined) setShowDepth(patch.showDepth);
              }}
            />
            <div className="map-chrome-bottom-right">
              <WindLegend visible={showWind && !!windSeries} />
            </div>
          </>
        )}

        {!mapBooted && (
          <div className="map-boot-overlay" aria-busy="true" aria-label="Loading chart">
            <div className="map-load-card">
              <span className="map-load-spin" aria-hidden />
              <div className="map-load-copy">
                <div className="map-load-title">Loading chart</div>
                <div className="map-load-sub">
                  {mapBootBounds ? "Framing your course…" : "Preparing the map…"}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

    </div>
  );
}
