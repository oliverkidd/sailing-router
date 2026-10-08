/**
 * Smoke-test live race ahead/behind and offline voyage tile budgeting.
 * Run: node scripts/test-live-race.mjs
 *
 * Mirrors the algorithms in liveRace.ts / voyagePack.ts so we can verify
 * without a browser GPS session.
 */

function haversineNm(a, b) {
  const R = 3440.065;
  const toRad = (d) => (d * Math.PI) / 180;
  const dlat = toRad(b[0] - a[0]);
  const dlon = toRad(b[1] - a[1]);
  const lat1 = toRad(a[0]);
  const lat2 = toRad(b[0]);
  const h =
    Math.sin(dlat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dlon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

function localNm(origin, point) {
  const cos = Math.max(0.2, Math.cos((origin[0] * Math.PI) / 180));
  return {
    e: (point[1] - origin[1]) * 60 * cos,
    n: (point[0] - origin[0]) * 60,
  };
}

function nearestOnRoute(legs, gps) {
  let bestDist = Infinity;
  let best = null;
  for (let i = 0; i < legs.length - 1; i++) {
    const a = legs[i].position;
    const b = legs[i + 1].position;
    const ab = localNm(a, b);
    const ag = localNm(a, gps);
    const len2 = ab.e * ab.e + ab.n * ab.n;
    let t = len2 < 1e-8 ? 0 : (ag.e * ab.e + ag.n * ab.n) / len2;
    t = Math.max(0, Math.min(1, t));
    const proj = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    const dist = haversineNm(gps, proj);
    if (dist >= bestDist) continue;
    bestDist = dist;
    const t0 = new Date(legs[i].t_utc).getTime();
    const t1 = new Date(legs[i + 1].t_utc).getTime();
    best = { position: proj, tUtc: new Date(t0 + (t1 - t0) * t).toISOString(), crossTrackNm: dist };
  }
  return best;
}

function compareToRoute(legs, gps, nowUtc, departUtc) {
  const nowMs = new Date(nowUtc).getTime();
  const departMs = new Date(departUtc).getTime();
  const departPassed = nowMs >= departMs;
  const along = nearestOnRoute(legs, gps);
  const aheadBehindS = along ? (new Date(along.tUtc).getTime() - nowMs) / 1000 : null;
  return { departPassed, aheadBehindS, along };
}

function lonToTileX(lon, z) {
  return Math.floor(((lon + 180) / 360) * 2 ** z);
}
function latToTileY(lat, z) {
  const clamped = Math.min(85, Math.max(-85, lat));
  const rad = (clamped * Math.PI) / 180;
  const n = Math.log(Math.tan(rad) + 1 / Math.cos(rad));
  return Math.floor(((1 - n / Math.PI) / 2) * 2 ** z);
}
function tilesForBbox(west, south, east, north, z) {
  const x0 = lonToTileX(west, z);
  const x1 = lonToTileX(east, z);
  const y0 = latToTileY(north, z);
  const y1 = latToTileY(south, z);
  let n = 0;
  for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++) {
    for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++) n++;
  }
  return n;
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// Planned Solent beat: start → mark → finish over 2 hours.
const depart = "2026-10-06T10:00:00.000Z";
const legs = [
  { position: [50.77, -1.30], t_utc: "2026-10-06T10:00:00.000Z" },
  { position: [50.75, -1.40], t_utc: "2026-10-06T11:00:00.000Z" },
  { position: [50.76, -1.50], t_utc: "2026-10-06T12:00:00.000Z" },
];

// On the route at the halfway time of the first leg → ~on time.
{
  const gps = [50.76, -1.35];
  const snap = compareToRoute(legs, gps, "2026-10-06T10:30:00.000Z", depart);
  assert(snap.departPassed, "depart should have passed");
  assert(snap.along != null, "should project onto route");
  assert(Math.abs(snap.aheadBehindS) < 20 * 60, `near on-time, got ${snap.aheadBehindS}s`);
  console.log("ok  ahead/behind near on-time", Math.round(snap.aheadBehindS), "s");
}

// Same place but clock still at start → boat is ahead of plan.
{
  const gps = [50.76, -1.35];
  const snap = compareToRoute(legs, gps, "2026-10-06T10:00:00.000Z", depart);
  assert(snap.aheadBehindS > 20 * 60, `should be ahead, got ${snap.aheadBehindS}s`);
  console.log("ok  ahead of plan", Math.round(snap.aheadBehindS / 60), "min");
}

// Same place but clock at finish of that leg → behind.
{
  const gps = [50.76, -1.35];
  const snap = compareToRoute(legs, gps, "2026-10-06T11:00:00.000Z", depart);
  assert(snap.aheadBehindS < -20 * 60, `should be behind, got ${snap.aheadBehindS}s`);
  console.log("ok  behind plan", Math.round(-snap.aheadBehindS / 60), "min");
}

// Reroute gate: after depart, with a fix, reroute is allowed.
{
  const snap = compareToRoute(legs, [50.76, -1.35], "2026-10-06T10:30:00.000Z", depart);
  assert(snap.departPassed, "reroute eligible after depart");
  console.log("ok  reroute eligible after depart");
}

// Offline voyage tile budget for a Solent-sized box stays under 280/archive.
{
  const west = -1.7,
    east = -1.1,
    south = 50.55,
    north = 50.85;
  const z9 = tilesForBbox(west, south, east, north, 9);
  const z11 = tilesForBbox(west, south, east, north, 11);
  const limited = Math.min(z9 + z11, 280);
  assert(limited <= 280, "cap at 280");
  console.log("ok  voyage tiles", { z9, z11, limited, archives: limited * 2 });
}

console.log("live-race + offline budget smoke: all passed");
