/** localStorage run history + request assembly for the routing lab. */

import { FALLBACK_COLORS } from "./colors";
import type {
  BoatConfig,
  Course,
  LatLon,
  RouteLeg,
  RouteRequest,
  TuningField,
  TuningSchemaResponse,
} from "./types";

export const LAB_RUNS_KEY = "masterwind-lab-runs-v1";
export const LAB_RUNS_MAX = 15;
export const LAB_CONCURRENCY = 3;

export type LabValues = Record<string, number | null>;

export interface LabCourseResult {
  elapsed_s: number;
  distance_nm: number;
  n_tacks: number;
  n_gybes: number;
  eta_utc?: string;
  unreachable: boolean;
  warnings: string[];
  legs: RouteLeg[];
  /** Advisory headland chain the router steered by, when guidance was on. */
  waypoints?: LatLon[];
  error?: string;
}

export interface LabRun {
  id: string;
  seq: number;
  label: string;
  timestamp: string;
  depart_utc: string;
  polar_id: string;
  params: LabValues;
  results: Record<string, LabCourseResult>;
}

export interface LabRunStore {
  nextSeq: number;
  runs: LabRun[];
}

export function emptyLabStore(): LabRunStore {
  return { nextSeq: 1, runs: [] };
}

export function loadLabStore(): LabRunStore {
  if (typeof window === "undefined") return emptyLabStore();
  try {
    const raw = localStorage.getItem(LAB_RUNS_KEY);
    if (!raw) return emptyLabStore();
    const parsed = JSON.parse(raw) as LabRunStore;
    if (!parsed || !Array.isArray(parsed.runs)) return emptyLabStore();
    return {
      nextSeq: Number(parsed.nextSeq) || parsed.runs.length + 1,
      runs: parsed.runs.slice(0, LAB_RUNS_MAX),
    };
  } catch {
    return emptyLabStore();
  }
}

export function saveLabStore(store: LabRunStore) {
  if (typeof window === "undefined") return;
  const payload: LabRunStore = {
    nextSeq: store.nextSeq,
    runs: store.runs.slice(0, LAB_RUNS_MAX),
  };
  try {
    localStorage.setItem(LAB_RUNS_KEY, JSON.stringify(payload));
  } catch {
    const trimmed = { ...payload, runs: payload.runs.slice(0, Math.max(1, payload.runs.length - 3)) };
    try {
      localStorage.setItem(LAB_RUNS_KEY, JSON.stringify(trimmed));
    } catch {
      /* quota — drop history rather than breaking the page */
      try {
        localStorage.removeItem(LAB_RUNS_KEY);
      } catch {
        /* ignore */
      }
    }
  }
}

export function appendLabRun(store: LabRunStore, run: Omit<LabRun, "id" | "seq" | "label">): {
  store: LabRunStore;
  run: LabRun;
} {
  const seq = store.nextSeq;
  const full: LabRun = {
    ...run,
    id: typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `run-${seq}-${Date.now()}`,
    seq,
    label: `Run ${seq}`,
  };
  const next: LabRunStore = {
    nextSeq: seq + 1,
    runs: [full, ...store.runs].slice(0, LAB_RUNS_MAX),
  };
  saveLabStore(next);
  return { store: next, run: full };
}

export async function runPool<T>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let i = 0;
  const n = Math.min(Math.max(1, concurrency), Math.max(1, items.length));
  await Promise.all(
    Array.from({ length: n }, async () => {
      for (;;) {
        const idx = i++;
        if (idx >= items.length) return;
        await worker(items[idx]);
      }
    }),
  );
}

export function colorForRun(seq: number): string {
  return FALLBACK_COLORS[(Math.max(1, seq) - 1) % FALLBACK_COLORS.length];
}

export function sameNum(a: number | null | undefined, b: number | null | undefined): boolean {
  if (a == null && b == null) return true;
  if (a == null || b == null) return false;
  return Number(a) === Number(b);
}

export function defaultsFromSchema(schema: TuningSchemaResponse): LabValues {
  const out: LabValues = {};
  for (const f of schema.fields) {
    out[f.name] = f.default ?? null;
  }
  return out;
}

export function fieldIsModified(field: TuningField, values: LabValues): boolean {
  const cur = values[field.name] === undefined ? field.default : values[field.name];
  return !sameNum(cur ?? null, field.default ?? null);
}

export function assembleRouteRequest(opts: {
  course: Course;
  boat: BoatConfig;
  schema: TuningSchemaResponse;
  values: LabValues;
  departUtc: string;
}): RouteRequest {
  const { schema, values, departUtc } = opts;
  const boat: BoatConfig = { ...opts.boat };
  let minDepth = opts.course.min_depth_m;
  let timeStep = 300;
  let headingStep = 7.5;
  const routerParams: Record<string, number> = {};

  for (const f of schema.fields) {
    const raw = values[f.name] === undefined ? f.default : values[f.name];
    if (f.group === "boat") {
      if (f.nullable && raw == null) {
        (boat as unknown as Record<string, number | null>)[f.name] = null;
      } else if (raw != null) {
        (boat as unknown as Record<string, number | null>)[f.name] = raw;
      }
    } else if (f.group === "course" && f.name === "min_depth_m") {
      if (raw != null && !sameNum(raw, f.default)) minDepth = raw;
    } else if (f.group === "request") {
      if (f.name === "time_step_s" && raw != null) timeStep = raw;
      if (f.name === "heading_step_deg" && raw != null) headingStep = raw;
    } else if (f.group === "router" && raw != null && !sameNum(raw, f.default)) {
      routerParams[f.name] = raw;
    }
  }

  return {
    course: { ...opts.course, min_depth_m: minDepth, depart_utc: departUtc },
    boat,
    model_ids: ["gfs"],
    time_step_s: timeStep,
    heading_step_deg: headingStep,
    router_params: Object.keys(routerParams).length ? routerParams : undefined,
  };
}

export function formatElapsed(s: number): string {
  if (!Number.isFinite(s)) return "—";
  const sec = Math.max(0, Math.round(s));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const r = sec % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${r}s`;
  return `${r}s`;
}

export function formatNm(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(2)} nm`;
}

export function formatDelta(n: number, digits = 0): string {
  if (!Number.isFinite(n) || n === 0) return "0";
  const abs = Math.abs(n);
  const body = digits > 0 ? abs.toFixed(digits) : String(Math.round(abs));
  return `${n > 0 ? "+" : "−"}${body}`;
}

export async function runPoolCourses<T>(
  items: T[],
  worker: (item: T) => Promise<void>,
): Promise<void> {
  return runPool(items, LAB_CONCURRENCY, worker);
}
