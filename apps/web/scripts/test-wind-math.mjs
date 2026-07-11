/**
 * Focused checks for wind bearing + grid sampling conventions.
 * Run: node apps/web/scripts/test-wind-math.mjs
 */

function flowBearingDeg(u, v) {
  return ((Math.atan2(u, v) * 180) / Math.PI + 360) % 360;
}

function twdFromUv(u, v) {
  return ((Math.atan2(-u, -v) * 180) / Math.PI + 360) % 360;
}

function sampleGrid(grid, lat, lon) {
  const { lat0, lon0, dlat, dlon, nlat, nlon, u, v, speed_kn } = grid;
  if (nlat < 2 || nlon < 2) {
    return { u: u[0] ?? 0, v: v[0] ?? 0, speed: speed_kn[0] ?? 0 };
  }
  const fi = Math.max(0, Math.min(nlat - 1.001, (lat - lat0) / dlat));
  const fj = Math.max(0, Math.min(nlon - 1.001, (lon - lon0) / dlon));
  const i0 = Math.floor(fi);
  const j0 = Math.floor(fj);
  const i1 = Math.min(i0 + 1, nlat - 1);
  const j1 = Math.min(j0 + 1, nlon - 1);
  const fy = fi - i0;
  const fx = fj - j0;
  const idx = (ii, jj) => ii * nlon + jj;
  const su =
    (1 - fy) * ((1 - fx) * u[idx(i0, j0)] + fx * u[idx(i0, j1)]) +
    fy * ((1 - fx) * u[idx(i1, j0)] + fx * u[idx(i1, j1)]);
  const sv =
    (1 - fy) * ((1 - fx) * v[idx(i0, j0)] + fx * v[idx(i0, j1)]) +
    fy * ((1 - fx) * v[idx(i1, j0)] + fx * v[idx(i1, j1)]);
  return { u: su, v: sv, speed: Math.hypot(su, sv) * 1.943844 };
}

function assertClose(actual, expected, eps, msg) {
  if (Math.abs(actual - expected) > eps) {
    throw new Error(`${msg}: got ${actual}, expected ${expected} (±${eps})`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// Flow-toward bearings for cardinal u/v (u=east, v=north).
assertClose(flowBearingDeg(0, 1), 0, 0.01, "north flow");
assertClose(flowBearingDeg(1, 0), 90, 0.01, "east flow");
assertClose(flowBearingDeg(0, -1), 180, 0.01, "south flow");
assertClose(flowBearingDeg(-1, 0), 270, 0.01, "west flow");

// Meteorological FROM is opposite of flow-toward.
assertClose(twdFromUv(0, 1), 180, 0.01, "northward flow => from south");
assertClose(twdFromUv(1, 0), 270, 0.01, "eastward flow => from west");

// Grid with west wind on left, east wind on right — mid sample should interpolate.
const grid = {
  lat0: 0,
  lon0: 0,
  dlat: 1,
  dlon: 1,
  nlat: 2,
  nlon: 2,
  // row0: west (-1,0) then east (1,0); row1 same
  u: [-1, 1, -1, 1],
  v: [0, 0, 0, 0],
  speed_kn: [1.94, 1.94, 1.94, 1.94],
};
const mid = sampleGrid(grid, 0.5, 0.5);
assertClose(mid.u, 0, 1e-6, "bilinear u at center");
assertClose(mid.v, 0, 1e-6, "bilinear v at center");
assertClose(flowBearingDeg(1, 0), 90, 0.01, "east sample bearing");

const left = sampleGrid(grid, 0.5, 0);
assert(left.u < 0, "left side should be westward u");
assertClose(flowBearingDeg(left.u, left.v), 270, 0.5, "left bearing west");

const right = sampleGrid(grid, 0.5, 1);
assert(right.u > 0, "right side should be eastward u");
assertClose(flowBearingDeg(right.u, right.v), 90, 0.5, "right bearing east");

// --- mergeSeries (mirrors meteoGrid.mergeSeries) ---
function mergeSeries(a, b) {
  const eps = 1e-6;
  const sameGrid =
    a.nlat === b.nlat &&
    a.nlon === b.nlon &&
    Math.abs(a.lat0 - b.lat0) < eps &&
    Math.abs(a.lon0 - b.lon0) < eps &&
    Math.abs(a.dlat - b.dlat) < eps &&
    Math.abs(a.dlon - b.dlon) < eps;
  if (!sameGrid) return b;
  const byTime = new Map();
  for (let i = 0; i < a.times.length; i++) {
    byTime.set(a.times[i], { u: a.u[i], v: a.v[i], speed_kn: a.speed_kn[i] });
  }
  for (let i = 0; i < b.times.length; i++) {
    byTime.set(b.times[i], { u: b.u[i], v: b.v[i], speed_kn: b.speed_kn[i] });
  }
  const times = Array.from(byTime.keys()).sort(
    (x, y) => new Date(x).getTime() - new Date(y).getTime(),
  );
  return {
    ...a,
    times,
    u: times.map((t) => byTime.get(t).u),
    v: times.map((t) => byTime.get(t).v),
    speed_kn: times.map((t) => byTime.get(t).speed_kn),
  };
}

function mkSeries(times) {
  return {
    lat0: 50,
    lon0: -2,
    dlat: 0.1,
    dlon: 0.1,
    nlat: 2,
    nlon: 2,
    times,
    u: times.map(() => [1, 1, 1, 1]),
    v: times.map(() => [0, 0, 0, 0]),
    speed_kn: times.map(() => [1.9, 1.9, 1.9, 1.9]),
  };
}

const merged = mergeSeries(
  mkSeries(["2026-07-11T00:00:00.000Z", "2026-07-11T01:00:00.000Z"]),
  mkSeries(["2026-07-11T01:00:00.000Z", "2026-07-11T02:00:00.000Z"]),
);
assert(merged.times.length === 3, "mergeSeries should dedupe to 3 times");
assert(merged.u.length === 3, "mergeSeries u frames");

// --- legAtTime (mirrors mapOverlays.legAtTime) ---
function legAtTime(legs, tUtc) {
  if (!legs.length) return null;
  const tMs = new Date(tUtc).getTime();
  if (tMs <= new Date(legs[0].t_utc).getTime()) {
    return { position: legs[0].position, index: 0 };
  }
  const last = legs.length - 1;
  if (tMs >= new Date(legs[last].t_utc).getTime()) {
    return { position: legs[last].position, index: last };
  }
  let i0 = 0;
  for (let i = 0; i < last; i++) {
    if (new Date(legs[i + 1].t_utc).getTime() >= tMs) {
      i0 = i;
      break;
    }
  }
  const a = legs[i0];
  const b = legs[i0 + 1];
  const t0 = new Date(a.t_utc).getTime();
  const t1 = new Date(b.t_utc).getTime();
  const ft = t1 === t0 ? 0 : (tMs - t0) / (t1 - t0);
  return {
    position: [
      a.position[0] + (b.position[0] - a.position[0]) * ft,
      a.position[1] + (b.position[1] - a.position[1]) * ft,
    ],
    index: i0,
  };
}

const legs = [
  { t_utc: "2026-07-11T00:00:00.000Z", position: [50, -1] },
  { t_utc: "2026-07-11T02:00:00.000Z", position: [51, -1] },
];
const midLeg = legAtTime(legs, "2026-07-11T01:00:00.000Z");
assertClose(midLeg.position[0], 50.5, 1e-6, "legAtTime mid lat");
assert(midLeg.index === 0, "legAtTime index");
const before = legAtTime(legs, "2026-07-10T23:00:00.000Z");
assert(before.index === 0 && before.position[0] === 50, "legAtTime clamps before");

console.log("wind math tests passed");
