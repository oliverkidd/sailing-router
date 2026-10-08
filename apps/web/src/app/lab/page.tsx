"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { LabMapRoute } from "@/components/LabMap";
import LabParamsPanel from "@/components/LabParamsPanel";
import LabTimeBar, { clockLabelUtc } from "@/components/LabTimeBar";
import { api } from "@/lib/api";
import { buildLabCourses, type LabCourse } from "@/lib/labCourses";
import { forecastShift } from "@/lib/labForecast";
import {
  appendLabRun,
  assembleRouteRequest,
  colorForRun,
  defaultsFromSchema,
  emptyLabStore,
  formatDelta,
  formatElapsed,
  formatNm,
  loadLabStore,
  runPoolCourses,
  type LabCourseResult,
  type LabRun,
  type LabRunStore,
  type LabValues,
} from "@/lib/labRuns";
import { currentForecastHourUtc, legAtTime } from "@/lib/mapOverlays";
import {
  fetchWindArea,
  hoursAheadIso,
  sampleGrid,
  sliceSeries,
  type MeteoGrid,
  type WindSeries,
} from "@/lib/meteoGrid";
import type { BoatConfig, RouteLeg, TuningSchemaResponse } from "@/lib/types";

const LabMap = dynamic(() => import("@/components/LabMap"), { ssr: false });

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

/** Covers every lab course (Needles Channel → Spithead, south coast → Solent). */
const LAB_BBOX = {
  lat_min: 50.42,
  lon_min: -1.72,
  lat_max: 50.86,
  lon_max: -1.05,
};

/** Long enough for the slowest course plus headroom for a re-run. */
const WIND_WINDOW_H = 12;
/** Mid-Solent probe for the headline TWD that orients the beat/run pair. */
const TWD_PROBE_LAT = 50.72;
const TWD_PROBE_LON = -1.35;
const HOUR_MS = 3_600_000;
/** Keep the scrubber usable before any route exists. */
const MIN_CLOCK_SPAN_H = 3;

type CardStatus = "idle" | "running" | "ok" | "error";

function twdFromUv(u: number, v: number): number {
  return ((Math.atan2(-u, -v) * 180) / Math.PI + 360) % 360;
}

function floorHourIso(iso: string): string {
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return iso;
  return new Date(Math.floor(ms / HOUR_MS) * HOUR_MS).toISOString();
}

export default function RoutingLabPage() {
  const [schema, setSchema] = useState<TuningSchemaResponse | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [values, setValues] = useState<LabValues>({});
  const [boat, setBoat] = useState<BoatConfig>(INITIAL_BOAT);
  const [twd, setTwd] = useState<number | null>(null);
  const [windNote, setWindNote] = useState("Fetching GFS wind…");
  const [windSeries, setWindSeries] = useState<WindSeries | null>(null);
  const [departUtc, setDepartUtc] = useState(currentForecastHourUtc);
  const [overlayTimeUtc, setOverlayTimeUtc] = useState(currentForecastHourUtc);
  const [playing, setPlaying] = useState(false);
  const [store, setStore] = useState<LabRunStore>(emptyLabStore);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<Record<string, CardStatus>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const runningLock = useRef(false);
  const storeRef = useRef(store);
  const valuesRef = useRef(values);
  const boatRef = useRef(boat);
  const schemaRef = useRef(schema);
  const twdRef = useRef(twd);
  storeRef.current = store;
  valuesRef.current = values;
  boatRef.current = boat;
  schemaRef.current = schema;
  twdRef.current = twd;

  const courses = useMemo(() => buildLabCourses(twd, departUtc), [twd, departUtc]);

  useEffect(() => {
    document.title = "Routing lab";
    setStore(loadLabStore());
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .tuningSchema()
      .then((s) => {
        if (cancelled) return;
        setSchema(s);
        setValues(defaultsFromSchema(s));
      })
      .catch((err: unknown) => {
        if (!cancelled) setSchemaError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (boat.polar_id) return;
    api
      .sampleBoat()
      .then((res) => setBoat({ ...INITIAL_BOAT, ...res.boat }))
      .catch(() => {
        /* polar stays empty — Run all stays disabled */
      });
  }, [boat.polar_id]);

  // One area fetch feeds both the headline TWD and every card's arrows.
  useEffect(() => {
    const ac = new AbortController();
    const hour = currentForecastHourUtc();
    fetchWindArea(
      LAB_BBOX,
      "gfs",
      hour,
      hoursAheadIso(hour, WIND_WINDOW_H),
      ac.signal,
      undefined,
      false,
    )
      .then((res) => {
        if (ac.signal.aborted) return;
        if (!res.series || res.series.times.length === 0) {
          setWindNote(res.error || "No GFS wind — using westerly 270°");
          return;
        }
        setWindSeries(res.series);
        const grid = sliceSeries(res.series, res.series.times[0]);
        const { u, v, speed } = sampleGrid(grid, TWD_PROBE_LAT, TWD_PROBE_LON);
        if (speed < 0.4) {
          setWindNote("GFS wind too light — using westerly 270°");
          return;
        }
        const deg = twdFromUv(u, v);
        setTwd(deg);
        setWindNote(
          `TWD ${deg.toFixed(0)}° · ${speed.toFixed(0)} kn · ${res.series.times.length} h loaded`,
        );
      })
      .catch((err: unknown) => {
        if (!ac.signal.aborted) {
          setWindNote(err instanceof Error ? err.message : "Wind fetch failed — using 270°");
        }
      });
    return () => ac.abort();
  }, []);

  const toggleRun = useCallback((id: string) => {
    setSelectedIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  }, []);

  const restoreRun = useCallback((run: LabRun) => {
    setValues({ ...run.params });
    if (run.polar_id) setBoat((b) => ({ ...b, polar_id: run.polar_id }));
  }, []);

  const runAll = useCallback(async () => {
    const schemaNow = schemaRef.current;
    const boatNow = boatRef.current;
    if (runningLock.current || !schemaNow || !boatNow.polar_id) return;
    runningLock.current = true;
    setRunning(true);
    const depart = currentForecastHourUtc();
    setDepartUtc(depart);
    setOverlayTimeUtc(depart);
    setPlaying(false);
    const courseList = buildLabCourses(twdRef.current, depart);
    setStatus(Object.fromEntries(courseList.map((c) => [c.id, "running"])));
    setErrors({});

    const results: Record<string, LabCourseResult> = {};
    await runPoolCourses(courseList, async (lab) => {
      try {
        const req = assembleRouteRequest({
          course: lab.course,
          boat: boatNow,
          schema: schemaNow,
          values: valuesRef.current,
          departUtc: depart,
        });
        const res = await api.route(req);
        const model = res.routes[0];
        if (!model) {
          results[lab.id] = {
            elapsed_s: 0,
            distance_nm: 0,
            n_tacks: 0,
            n_gybes: 0,
            unreachable: true,
            warnings: res.warnings,
            legs: [],
            error: "No route returned",
          };
          setStatus((s) => ({ ...s, [lab.id]: "error" }));
          setErrors((e) => ({ ...e, [lab.id]: "No route returned" }));
          return;
        }
        const warnings = [...(res.warnings ?? []), ...(model.warnings ?? [])];
        results[lab.id] = {
          elapsed_s: model.elapsed_s,
          distance_nm: model.distance_nm,
          n_tacks: model.n_tacks,
          n_gybes: model.n_gybes,
          eta_utc: model.eta_utc,
          unreachable: Boolean(model.unreachable),
          warnings,
          legs: model.legs,
          waypoints: model.waypoints ?? undefined,
        };
        setStatus((s) => ({ ...s, [lab.id]: model.unreachable ? "error" : "ok" }));
        if (model.unreachable) {
          setErrors((e) => ({ ...e, [lab.id]: warnings[0] || "Unreachable" }));
        }
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        results[lab.id] = {
          elapsed_s: 0,
          distance_nm: 0,
          n_tacks: 0,
          n_gybes: 0,
          unreachable: true,
          warnings: [],
          legs: [],
          error: msg,
        };
        setStatus((s) => ({ ...s, [lab.id]: "error" }));
        setErrors((e) => ({ ...e, [lab.id]: msg }));
      }
    });

    const { store: next, run } = appendLabRun(storeRef.current, {
      timestamp: new Date().toISOString(),
      depart_utc: depart,
      polar_id: boatNow.polar_id,
      params: { ...valuesRef.current },
      results,
    });
    setStore(next);
    setSelectedIds((ids) => {
      const prev = ids[0];
      return prev && prev !== run.id ? [run.id, prev] : [run.id];
    });
    setRunning(false);
    runningLock.current = false;
  }, []);

  const selectedRuns = useMemo(() => {
    const set = new Set(selectedIds);
    return store.runs.filter((r) => set.has(r.id)).sort((a, b) => a.seq - b.seq);
  }, [store.runs, selectedIds]);

  // Clock spans depart → the latest ETA on screen, so every card shares one hour.
  const clockEndUtc = useMemo(() => {
    const departMs = new Date(departUtc).getTime();
    let latest = departMs + MIN_CLOCK_SPAN_H * HOUR_MS;
    for (const run of selectedRuns) {
      for (const res of Object.values(run.results)) {
        const legs = res.legs;
        if (!legs?.length) continue;
        const end = new Date(legs[legs.length - 1].t_utc).getTime();
        if (Number.isFinite(end) && end > latest) latest = end;
      }
    }
    return new Date(latest).toISOString();
  }, [departUtc, selectedRuns]);

  const windGrid = useMemo(
    () => (windSeries ? sliceSeries(windSeries, overlayTimeUtc) : null),
    [windSeries, overlayTimeUtc],
  );

  const scrubHourUtc = useMemo(() => floorHourIso(overlayTimeUtc), [overlayTimeUtc]);

  const canRun = Boolean(schema && boat.polar_id) && !running;

  return (
    <div className="lab-shell">
      <aside className="lab-rail">
        <h1>Routing lab</h1>
        <p className="lab-sub">Internal tuning page — not linked from the main app.</p>
        {schemaError && <p className="lab-error">{schemaError}</p>}
        {!schema && !schemaError && <p className="lab-muted">Loading parameter schema…</p>}
        {schema && (
          <LabParamsPanel
            schema={schema}
            values={values}
            onChange={setValues}
            onResetAll={() => setValues(defaultsFromSchema(schema))}
          />
        )}
        <section className="lab-history">
          <h2>Runs</h2>
          {store.runs.length === 0 && <p className="lab-muted">No runs yet.</p>}
          <ul className="lab-history-list">
            {store.runs.map((run) => (
              <li key={run.id} className="lab-history-item">
                <div className="lab-history-head">
                  <span className="lab-run-dot" style={{ background: colorForRun(run.seq) }} />
                  <strong>{run.label}</strong>
                  <span className="lab-muted">
                    {new Date(run.timestamp).toLocaleTimeString(undefined, {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                  <button type="button" className="rail-section-action" onClick={() => restoreRun(run)}>
                    Restore
                  </button>
                </div>
                <details>
                  <summary>Param snapshot</summary>
                  <pre className="lab-snapshot">{JSON.stringify(run.params, null, 2)}</pre>
                </details>
              </li>
            ))}
          </ul>
        </section>
      </aside>
      <main className="lab-main">
        <div className="lab-toolbar">
          <div className="lab-toolbar-meta">
            <span>GFS</span>
            <span>{windNote}</span>
            <span>Depart {clockLabelUtc(departUtc)}</span>
            {!boat.polar_id && <span className="lab-muted">Loading polar…</span>}
          </div>
          <button type="button" className="lab-run-btn" disabled={!canRun} onClick={() => void runAll()}>
            {running ? "Running…" : "Run all"}
          </button>
        </div>
        <div className="lab-clockbar">
          <LabTimeBar
            startUtc={departUtc}
            endUtc={clockEndUtc}
            valueUtc={overlayTimeUtc}
            playing={playing}
            onChange={setOverlayTimeUtc}
            onPlayingChange={setPlaying}
            note={windSeries ? undefined : "no wind loaded — arrows hidden"}
          />
        </div>
        <div className="lab-grid">
          {courses.map((lab) => (
            <CourseCard
              key={lab.id}
              lab={lab}
              runs={store.runs}
              selectedRuns={selectedRuns}
              selectedIds={selectedIds}
              status={status[lab.id] ?? "idle"}
              error={errors[lab.id]}
              onToggle={toggleRun}
              windGrid={windGrid}
              overlayTimeUtc={overlayTimeUtc}
              scrubHourUtc={scrubHourUtc}
            />
          ))}
        </div>
      </main>
    </div>
  );
}

interface CardRoute extends LabMapRoute {
  boat: RouteLeg | null;
}

function CourseCard({
  lab,
  runs,
  selectedRuns,
  selectedIds,
  status,
  error,
  onToggle,
  windGrid,
  overlayTimeUtc,
  scrubHourUtc,
}: {
  lab: LabCourse;
  runs: LabRun[];
  selectedRuns: LabRun[];
  selectedIds: string[];
  status: CardStatus;
  error?: string;
  onToggle: (id: string) => void;
  windGrid: MeteoGrid | null;
  overlayTimeUtc: string;
  scrubHourUtc: string;
}) {
  const cardRoutes = useMemo(() => {
    const out: CardRoute[] = [];
    for (const run of selectedRuns) {
      const res = run.results[lab.id];
      if (!res?.legs?.length) continue;
      const at = legAtTime(res.legs, overlayTimeUtc);
      out.push({
        id: run.id,
        color: colorForRun(run.seq),
        legs: res.legs,
        playhead: at ? { position: at.position, heading: at.heading ?? 0, index: at.index } : null,
        waypoints: res.waypoints ?? null,
        boat: at?.leg ?? null,
      });
    }
    return out;
  }, [selectedRuns, lab.id, overlayTimeUtc]);

  const latestLegs = cardRoutes.length ? cardRoutes[cardRoutes.length - 1].legs : null;
  const shift = useMemo(() => (latestLegs ? forecastShift(latestLegs) : null), [latestLegs]);

  return (
    <article className="lab-card">
      <header className="lab-card-head">
        <div>
          <h2>{lab.title}</h2>
          <p>{lab.blurb}</p>
        </div>
        <span className={`lab-status lab-status-${status}`}>
          {status === "running" ? "routing" : status === "ok" ? "ok" : status === "error" ? "error" : ""}
        </span>
      </header>
      <div className="lab-card-map-wrap">
        <LabMap course={lab.course} routes={cardRoutes} windGrid={windGrid} />
        {status === "running" && <div className="lab-map-overlay">Routing…</div>}
      </div>
      {cardRoutes.some((r) => r.boat) && (
        <div className="lab-boat-readout">
          {cardRoutes.map((r) =>
            r.boat ? (
              <span key={r.id} className="lab-boat-line">
                <span className="lab-run-dot" style={{ background: r.color }} />
                TWD {Math.round(r.boat.twd)}° · {r.boat.tws.toFixed(1)} kn · TWA{" "}
                {Math.round(Math.abs(r.boat.twa))}° · {r.boat.sog.toFixed(1)} kn SOG
              </span>
            ) : null,
          )}
        </div>
      )}
      {shift && shift.buckets.length > 0 && (
        <div className="lab-hours">
          {shift.buckets.map((b) => (
            <span
              key={b.hourUtc}
              className={`lab-hour${b.hourUtc === scrubHourUtc ? " on" : ""}`}
              title={`${b.label}: sailed ${b.distanceNm.toFixed(2)} nm in TWD ${b.twdDeg.toFixed(0)}° / ${b.twsKn.toFixed(1)} kn`}
            >
              <strong>{b.label}</strong> {b.distanceNm.toFixed(1)} nm · {Math.round(b.twdDeg)}°
            </span>
          ))}
          <span className="lab-muted">
            {shift.crossings} hour {shift.crossings === 1 ? "crossing" : "crossings"} · shift{" "}
            {Math.round(shift.maxShiftDeg)}°
          </span>
        </div>
      )}
      <div className="lab-chips">
        {runs.map((run) => (
          <label key={run.id} className={`lab-chip${selectedIds.includes(run.id) ? " on" : ""}`}>
            <span className="lab-run-dot" style={{ background: colorForRun(run.seq) }} />
            <input type="checkbox" checked={selectedIds.includes(run.id)} onChange={() => onToggle(run.id)} />
            {run.label}
          </label>
        ))}
        {runs.length === 0 && <span className="lab-muted">No runs yet</span>}
      </div>
      <div className="lab-metrics">
        {selectedRuns.map((run, i) => {
          const res = run.results[lab.id];
          const prev = i > 0 ? selectedRuns[i - 1].results[lab.id] : undefined;
          if (!res) {
            return (
              <div key={run.id} className="lab-metric-row">
                <span className="lab-run-dot" style={{ background: colorForRun(run.seq) }} />
                <span>{run.label}</span>
                <span className="lab-muted">no result</span>
              </div>
            );
          }
          if (res.error) {
            return (
              <div key={run.id} className="lab-metric-row">
                <span className="lab-run-dot" style={{ background: colorForRun(run.seq) }} />
                <span>{run.label}</span>
                <span className="lab-error">{res.error}</span>
              </div>
            );
          }
          const dElapsed = prev && !prev.error ? res.elapsed_s - prev.elapsed_s : null;
          const dNm = prev && !prev.error ? res.distance_nm - prev.distance_nm : null;
          const dTack = prev && !prev.error ? res.n_tacks - prev.n_tacks : null;
          const dGybe = prev && !prev.error ? res.n_gybes - prev.n_gybes : null;
          return (
            <div key={run.id} className="lab-metric-row">
              <span className="lab-run-dot" style={{ background: colorForRun(run.seq) }} />
              <span>{run.label}</span>
              <span>
                {formatElapsed(res.elapsed_s)}
                {dElapsed != null && (
                  <em className={dElapsed <= 0 ? "better" : "worse"}> {formatDelta(dElapsed)}s</em>
                )}
              </span>
              <span>
                {formatNm(res.distance_nm)}
                {dNm != null && <em className={dNm <= 0 ? "better" : "worse"}> {formatDelta(dNm, 2)}</em>}
              </span>
              <span>
                {res.n_tacks} t / {res.n_gybes} g
                {dTack != null && <em className={dTack <= 0 ? "better" : "worse"}> t{formatDelta(dTack)}</em>}
                {dGybe != null && <em className={dGybe <= 0 ? "better" : "worse"}> g{formatDelta(dGybe)}</em>}
              </span>
              {res.unreachable && <span className="lab-error">unreachable</span>}
            </div>
          );
        })}
      </div>
      {error && status !== "running" && <p className="lab-error lab-card-error">{error}</p>}
    </article>
  );
}
