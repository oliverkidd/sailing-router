// One command to run backend + frontend together. Ctrl-C stops both.
import { spawn } from "node:child_process";
import process from "node:process";
import { ensureBackend, ensureNode, paths } from "./setup.mjs";

ensureNode();
ensureBackend();

console.log("\n▶ API  → http://localhost:8000   ▶ Web → http://localhost:3000\n");

const child = spawn(
  "npx",
  [
    "concurrently",
    "-k",
    "-n", "api,web",
    "-c", "blue.bold,green.bold",
    "cd services/api && .venv/bin/uvicorn app.main:app --reload --port 8000",
    "npm run dev --workspace apps/web",
  ],
  { cwd: paths.root, stdio: "inherit", env: process.env }
);

const stop = () => child.kill("SIGINT");
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
child.on("exit", (code) => process.exit(code ?? 0));
