// Idempotent local bootstrap: Python venv + backend deps, and node workspace deps.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import process from "node:process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const apiDir = path.join(root, "services", "api");
const venvBin = path.join(apiDir, ".venv", "bin");
const venvPy = path.join(venvBin, "python");

function sh(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: "inherit", ...opts });
  if (r.status !== 0) {
    console.error(`\n[setup] command failed: ${cmd} ${args.join(" ")}`);
    process.exit(r.status ?? 1);
  }
}

function pythonVersion(cmd) {
  const r = spawnSync(cmd, ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], {
    encoding: "utf8",
  });
  if (r.status !== 0) return null;
  const [major, minor] = String(r.stdout).trim().split(".").map(Number);
  if (!Number.isFinite(major) || !Number.isFinite(minor)) return null;
  return { major, minor };
}

function findPython() {
  // Prefer versioned binaries first — macOS /usr/bin/python3 is often too old (3.9).
  for (const c of ["python3.13", "python3.12", "python3.11", "python3"]) {
    const v = pythonVersion(c);
    if (v && (v.major > 3 || (v.major === 3 && v.minor >= 11))) return c;
  }
  console.error(
    "[setup] Python 3.11+ not found. Install it (e.g. `brew install python@3.12`) and retry.",
  );
  process.exit(1);
}

function venvIsOk() {
  if (!existsSync(venvPy)) return false;
  const v = pythonVersion(venvPy);
  return Boolean(v && (v.major > 3 || (v.major === 3 && v.minor >= 11)));
}

export function ensureBackend() {
  if (venvIsOk()) return;

  if (existsSync(venvPy)) {
    console.log("[setup] existing .venv uses Python <3.11 — recreating…");
    sh("rm", ["-rf", ".venv"], { cwd: apiDir });
  }

  const py = findPython();
  console.log(`[setup] creating Python venv with ${py} + installing backend deps…`);
  sh(py, ["-m", "venv", ".venv"], { cwd: apiDir });
  sh(venvPy, ["-m", "pip", "install", "-q", "--upgrade", "pip"], { cwd: apiDir });
  sh(venvPy, ["-m", "pip", "install", "-q", "-e", ".[dev]"], { cwd: apiDir });
}

export function ensureNode() {
  if (!existsSync(path.join(root, "node_modules", "next"))) {
    console.log("[setup] installing node deps via npm workspaces (first run)…");
    sh("npm", ["install"], { cwd: root });
  }
}

export const paths = { root, apiDir, venvBin, venvPy };

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  ensureNode();
  ensureBackend();
  console.log("[setup] done.");
}
