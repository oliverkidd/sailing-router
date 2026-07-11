"use client";

export type ModelStatus = "idle" | "fetching" | "ready" | "error";

/** Transient route-build status — only while preparing/routing or on error. */
export default function RouteBuildProgress({
  prepState,
  running,
  routeCount,
  message,
}: {
  prepState: "idle" | "preparing" | "ready" | "error";
  running: boolean;
  routeCount: number;
  message?: string;
}) {
  let phase = "";
  if (prepState === "preparing") phase = "Preparing route data…";
  else if (running) phase = "Routing…";
  else if (prepState === "error") phase = message || "Route prepare failed";
  else if (message && routeCount === 0 && !running) phase = message;
  else return null;

  const isError = prepState === "error" || (routeCount === 0 && !!message && !running);

  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 600,
        color: isError ? "#f87171" : "var(--text)",
        flexShrink: 0,
      }}
    >
      {phase}
    </div>
  );
}
