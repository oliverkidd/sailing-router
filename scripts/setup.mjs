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

function findPython() {
  for (const c of ["python3", "python3.12", "python3.11"]) {
    const r = spawnSync(c, ["--version"], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  console.error("[setup] Python 3.11+ not found. Install Python 3 and retry.");
  process.exit(1);
}

export function ensureBackend() {
  if (!existsSync(venvPy)) {
    const py = findPython();
    console.log("[setup] creating Python venv + installing backend deps (first run)…");
    sh(py, ["-m", "venv", ".venv"], { cwd: apiDir });
    sh(venvPy, ["-m", "pip", "install", "-q", "--upgrade", "pip"], { cwd: apiDir });
    sh(venvPy, ["-m", "pip", "install", "-q", "-e", ".[dev]"], { cwd: apiDir });
  }
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
