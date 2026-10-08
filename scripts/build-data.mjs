#!/usr/bin/env node
/**
 * Smart offline asset builder for MasterWind.
 *
 * Idempotent: skips artifacts that already exist (with size/source checks).
 * Parallelises independent steps; sequences only true dependencies.
 *
 * Usage:
 *   npm run build-data              # full chart region
 *   npm run build-data -- --sample  # Solent sample (faster)
 *   npm run build-data -- --cyclades  # Kea/Kythnos/Syros WindNinja test
 *   npm run build-data -- --only wind-terrain
 *   npm run build-data -- --force wind-terrain
 *   npm run build-data -- --only terrain-rgb
 *   npm run build-data -- --only install-windninja
 *
 * Does NOT re-download GB-scale OSM/EMODnet sources — assumes those are present.
 * Does NOT run automatically — invoke it yourself.
 */
import { spawn } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import process from "node:process";
import { ensureBackend, paths } from "./setup.mjs";

const root = paths.root;
const apiDir = paths.apiDir;
const venvPy = paths.venvPy;

const args = process.argv.slice(2);
const SAMPLE = args.includes("--sample");
const CYCLADES = args.includes("--cyclades");
const FORCE = new Set();
const ONLY = new Set();
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--force" && args[i + 1]) FORCE.add(args[++i]);
  if (args[i] === "--only" && args[i + 1]) ONLY.add(args[++i]);
  if (args[i].startsWith("--force=")) FORCE.add(args[i].slice(8));
  if (args[i].startsWith("--only=")) ONLY.add(args[i].slice(7));
}

function regionFlags() {
  if (CYCLADES) return ["--cyclades"];
  if (SAMPLE) return ["--sample"];
  return [];
}

const ARTIFACTS = {
  coastline: path.join(root, "apps/web/public/geo/coastline.pmtiles"),
  depthTiles: path.join(root, "apps/web/public/geo/depth.pmtiles"),
  depthMosaic: path.join(root, "data/geo/bathymetry/region_depth.tif"),
  landChart: path.join(root, "data/geo/osm/land_chart.geojson"),
  landGeojsonl: path.join(root, "data/cache/tiles_build/land.geojsonl"),
  osmShp: path.join(root, "data/geo/osm/land-polygons-split-4326/land_polygons.shp"),
  demDir: path.join(root, "data/dem"),
  windTerrain: path.join(root, "data/cache/wind_terrain/region/library.zarr"),
  windStamp: path.join(root, "data/cache/wind_terrain/region/.complete"),
  windRegionStamp: path.join(root, "data/cache/wind_terrain/.region_complete"),
  windNinjaBin: path.join(root, "tools/windninja/bin/WindNinja_cli"),
  windNinjaWrapper: path.join(root, "tools/windninja/bin/WindNinja"),
  windNinjaData: path.join(root, "tools/windninja/data"),
  terrainRgb: path.join(root, "apps/web/public/geo/terrain/tilejson.json"),
  terrainRgbStamp: path.join(root, "data/cache/tiles_build/terrain_rgb/.region_complete"),
  demVrt: path.join(root, "data/dem/region_dem.vrt"),
  demStamp: path.join(root, "data/dem/.region_complete"),
};

function mb(p) {
  try {
    return statSync(p).size / (1024 * 1024);
  } catch {
    return 0;
  }
}

function fileOk(p, minMb = 0.001) {
  return existsSync(p) && mb(p) >= minMb;
}

function demOk() {
  if (SAMPLE || CYCLADES) {
    if (!existsSync(ARTIFACTS.demDir)) return false;
    return readdirSync(ARTIFACTS.demDir).some(
      (f) => /\.tiff?$/i.test(f) && mb(path.join(ARTIFACTS.demDir, f)) > 0.1,
    );
  }
  return existsSync(ARTIFACTS.demStamp) || existsSync(ARTIFACTS.demVrt);
}

function windTerrainOk() {
  if (!SAMPLE && !CYCLADES) {
    return existsSync(ARTIFACTS.windRegionStamp);
  }
  if (!existsSync(ARTIFACTS.windTerrain)) return false;
  if (existsSync(ARTIFACTS.windStamp)) {
    try {
      const meta = JSON.parse(readFileSync(ARTIFACTS.windStamp, "utf8"));
      // Identity-only bakes are incomplete for the real pipeline.
      return meta.source === "windninja";
    } catch {
      /* fall through */
    }
  }
  const attrsPath = path.join(ARTIFACTS.windTerrain, ".zattrs");
  if (existsSync(attrsPath)) {
    try {
      const attrs = JSON.parse(readFileSync(attrsPath, "utf8"));
      return attrs.source === "windninja";
    } catch {
      return false;
    }
  }
  return false;
}

function windNinjaOk() {
  if (process.env.WINDNINJA_BIN && existsSync(process.env.WINDNINJA_BIN)) return true;
  return fileOk(ARTIFACTS.windNinjaBin, 0.01);
}

function findWindNinja() {
  if (process.env.WINDNINJA_BIN && existsSync(process.env.WINDNINJA_BIN)) {
    return process.env.WINDNINJA_BIN;
  }
  if (fileOk(ARTIFACTS.windNinjaWrapper, 0.001)) return ARTIFACTS.windNinjaWrapper;
  if (fileOk(ARTIFACTS.windNinjaBin, 0.01)) return ARTIFACTS.windNinjaBin;
  return null;
}

function windEnv() {
  const env = { ...process.env };
  if (existsSync(ARTIFACTS.windNinjaData)) {
    env.WINDNINJA_DATA = env.WINDNINJA_DATA || ARTIFACTS.windNinjaData;
  } else {
    const srcData = path.join(root, "tools/windninja/src/windninja/data");
    if (existsSync(srcData)) env.WINDNINJA_DATA = env.WINDNINJA_DATA || srcData;
  }
  if (!env.GDAL_DATA) {
    if (existsSync("/opt/homebrew/share/gdal")) env.GDAL_DATA = "/opt/homebrew/share/gdal";
    else if (existsSync("/usr/local/share/gdal")) env.GDAL_DATA = "/usr/local/share/gdal";
  }
  return env;
}

function runCmd(cmd, cmdArgs, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, cmdArgs, {
      stdio: "inherit",
      ...opts,
      env: {
        ...windEnv(),
        CHART_SAMPLE: SAMPLE ? "1" : process.env.CHART_SAMPLE,
        CHART_CYCLADES: CYCLADES ? "1" : process.env.CHART_CYCLADES,
        ...(opts.env || {}),
      },
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${cmd} ${cmdArgs.join(" ")} exited ${code}`));
    });
  });
}

function runPy(scriptArgs) {
  return runCmd(venvPy, scriptArgs, { cwd: apiDir });
}

const steps = {
  "land-chart": {
    title: "Routing land mask (land_chart.geojson)",
    ready: () => fileOk(ARTIFACTS.landChart, 0.01),
    canRun: () => fileOk(ARTIFACTS.landGeojsonl, 0.01) || fileOk(ARTIFACTS.osmShp, 1),
    run: () => runPy(["scripts/build_route_land.py", ...regionFlags()]),
  },
  dem: {
    title: "Copernicus DEM GLO-30 (data/dem)",
    ready: () => demOk(),
    canRun: () => true,
    run: () => runPy(["scripts/fetch_dem.py", ...regionFlags()]),
  },
  coastline: {
    title: "Coastline PMTiles",
    ready: () => fileOk(ARTIFACTS.coastline, 10),
    canRun: () => fileOk(ARTIFACTS.osmShp, 1) || fileOk(ARTIFACTS.landGeojsonl, 0.01),
    run: () => runPy(["scripts/build_coastline_tiles.py", ...regionFlags()]),
  },
  depth: {
    // Mosaic is the routing source of truth. Skip when mosaic is present even if
    // depth.pmtiles is tiny/stale — avoid multi-hour EMODnet rebuilds.
    title: "Depth mosaic + PMTiles",
    ready: () => fileOk(ARTIFACTS.depthMosaic, 50),
    canRun: () => true,
    run: () => runPy(["scripts/build_depth_tiles.py", ...regionFlags()]),
  },
  "wind-terrain": {
    title: "WindNinja terrain-response library (400 m)",
    ready: () => windTerrainOk(),
    canRun: () => demOk(),
    run: async () => {
      // Clear identity / incomplete library so we rewrite cleanly.
      // Never delete the Aegean region mosaic during a full-chart bake.
      if (
        (SAMPLE || CYCLADES) &&
        existsSync(ARTIFACTS.windTerrain) &&
        !windTerrainOk()
      ) {
        console.log("  clearing incomplete/identity terrain library…");
        rmSync(ARTIFACTS.windTerrain, { recursive: true, force: true });
        if (existsSync(ARTIFACTS.windStamp)) rmSync(ARTIFACTS.windStamp);
      }
      const bin = findWindNinja();
      const pyArgs = ["scripts/build_wind_terrain.py", ...regionFlags()];
      if (bin) {
        pyArgs.push("--windninja-bin", bin);
      } else if (SAMPLE || CYCLADES) {
        console.warn(
          "  ⚠ WindNinja CLI not found — writing identity library.\n" +
            "    Then: npm run build-data -- --only install-windninja\n" +
            "    Then: npm run build-data -- --force wind-terrain",
        );
        pyArgs.push("--identity");
      } else {
        throw new Error(
          "WindNinja CLI not found — full-chart bake requires the real solver",
        );
      }
      await runPy(pyArgs);
      if (SAMPLE || CYCLADES) {
        mkdirSync(path.dirname(ARTIFACTS.windStamp), { recursive: true });
        writeFileSync(
          ARTIFACTS.windStamp,
          JSON.stringify(
            {
              source: bin ? "windninja" : "identity-forced",
              at: new Date().toISOString(),
              sample: SAMPLE,
              cyclades: CYCLADES,
            },
            null,
            2,
          ),
        );
      }
    },
  },
  "terrain-rgb": {
    title: "MapLibre Terrain-RGB tiles (full chart, 1° COGs)",
    ready: () =>
      SAMPLE || CYCLADES
        ? fileOk(ARTIFACTS.terrainRgb, 0.0001)
        : existsSync(ARTIFACTS.terrainRgbStamp),
    canRun: () => demOk(),
    run: () => runPy(["scripts/build_terrain_rgb.py", ...regionFlags()]),
  },
  "install-windninja": {
    title: "Build WindNinja CLI into tools/windninja",
    ready: () => windNinjaOk(),
    canRun: () => true,
    run: () => runCmd("bash", [path.join(root, "scripts/install-windninja.sh")], { cwd: root }),
  },
};

function wants(id) {
  if (ONLY.size > 0) return ONLY.has(id);
  // install-windninja is pulled in automatically when wind-terrain needs it
  if (id === "install-windninja") return false;
  return true;
}

function shouldForce(id) {
  return FORCE.has(id) || FORCE.has("all");
}

async function runStep(id) {
  const step = steps[id];
  const t0 = Date.now();
  const label = `[${id}]`;
  if (!shouldForce(id) && step.ready()) {
    console.log(`${label} ✓ already present — skip (${step.title})`);
    return { id, status: "skip", ms: 0 };
  }
  if (!step.canRun()) {
    console.log(`${label} ✗ missing inputs — skip (${step.title})`);
    return { id, status: "blocked", ms: 0 };
  }
  console.log(`${label} ▶ ${step.title}…`);
  try {
    await step.run();
    const ms = Date.now() - t0;
    console.log(`${label} ✓ done in ${(ms / 1000).toFixed(1)}s`);
    return { id, status: "built", ms };
  } catch (err) {
    const ms = Date.now() - t0;
    console.error(`${label} ✗ failed after ${(ms / 1000).toFixed(1)}s:`, err.message || err);
    return { id, status: "error", ms, error: String(err.message || err) };
  }
}

async function runParallel(ids) {
  return Promise.all(ids.map((id) => runStep(id)));
}

async function main() {
  console.log("══ MasterWind build-data ══");
  console.log(
    `mode: ${CYCLADES ? "cyclades" : SAMPLE ? "sample (Solent)" : "full region"}`,
  );
  if (ONLY.size) console.log(`only: ${[...ONLY].join(", ")}`);
  if (FORCE.size) console.log(`force: ${[...FORCE].join(", ")}`);
  console.log("");

  ensureBackend();

  // Opt-in or automatic WindNinja install before terrain bake
  const needTerrain = wants("wind-terrain");
  if (ONLY.has("install-windninja")) {
    const r = await runStep("install-windninja");
    if (r.status === "error") process.exit(1);
    if (!needTerrain && ONLY.size === 1) {
      console.log("\n[build-data] done.");
      return;
    }
  }

  // Wave 1 — independent (parallel)
  const wave1 = ["land-chart", "dem", "coastline", "depth"].filter(wants);
  const r1 = await runParallel(wave1);

  // Wave 2 — Terrain-RGB then WindNinja (sequential on the full chart).
  const wave2 = ["terrain-rgb", "wind-terrain"].filter(wants);
  if (wave2.includes("wind-terrain") && !windNinjaOk() && process.env.BUILD_WINDNINJA !== "0") {
    console.log("\n[build-data] WindNinja missing — attempting local CLI build…");
    await runStep("install-windninja");
  }
  const r2 =
    SAMPLE || CYCLADES
      ? await runParallel(wave2)
      : await (async () => {
          const out = [];
          for (const id of wave2) out.push(await runStep(id));
          return out;
        })();

  const all = [...r1, ...r2];
  console.log("\n══ Summary ══");
  for (const r of all) {
    const sec = r.ms ? ` (${(r.ms / 1000).toFixed(1)}s)` : "";
    console.log(`  ${r.status.padEnd(8)} ${r.id}${sec}`);
  }
  const errors = all.filter((r) => r.status === "error");
  if (errors.length) {
    console.error(`\n${errors.length} step(s) failed.`);
    process.exit(1);
  }
  console.log("\n[build-data] done.");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
