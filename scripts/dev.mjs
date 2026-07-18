// One command to run backend + frontend together. Ctrl-C stops both.
import { spawn, spawnSync } from "node:child_process";
import process from "node:process";
import { ensureBackend, ensureNode, paths } from "./setup.mjs";

const API_PORT = Number(process.env.API_PORT || 8000);
const WEB_PORT = Number(process.env.WEB_PORT || 3000);

function portHolder(port) {
  const r = spawnSync("lsof", ["-nP", `-iTCP:${port}`, "-sTCP:LISTEN"], {
    encoding: "utf8",
  });
  if (r.status !== 0 || !r.stdout) return null;
  const lines = r.stdout.trim().split("\n").slice(1);
  if (!lines.length) return null;
  const cols = lines[0].trim().split(/\s+/);
  return { command: cols[0], pid: cols[1] };
}

function assertPortsFree(ports) {
  const busy = ports
    .map((port) => ({ port, holder: portHolder(port) }))
    .filter((x) => x.holder);
  if (!busy.length) return;
  for (const { port, holder } of busy) {
    console.error(
      `[dev] port ${port} is already in use by ${holder.command} (pid ${holder.pid})`,
    );
  }
  const pids = [...new Set(busy.map((x) => x.holder.pid))].join(" ");
  console.error(`[dev] free them with: kill ${pids}`);
  console.error(`[dev] or override ports: API_PORT=8001 WEB_PORT=3001 npm run dev`);
  process.exit(1);
}

ensureNode();
ensureBackend();
assertPortsFree([API_PORT, WEB_PORT]);

console.log(`\n▶ API  → http://localhost:${API_PORT}   ▶ Web → http://localhost:${WEB_PORT}\n`);

const child = spawn(
  "npx",
  [
    "concurrently",
    "-k",
    "-n", "api,web",
    "-c", "blue.bold,green.bold",
    `cd services/api && .venv/bin/uvicorn app.main:app --reload --port ${API_PORT}`,
    "npm run dev --workspace apps/web",
  ],
  {
    cwd: paths.root,
    stdio: "inherit",
    env: {
      ...process.env,
      PORT: String(WEB_PORT),
      NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? `http://localhost:${API_PORT}`,
    },
  },
);

const stop = () => child.kill("SIGINT");
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
child.on("exit", (code) => process.exit(code ?? 0));
