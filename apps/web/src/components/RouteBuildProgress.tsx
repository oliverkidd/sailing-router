"use client";

export type ModelStatus = "idle" | "fetching" | "ready" | "error";

/** Transient route-build status — only while preparing/routing or on error/warning. */
export default function RouteBuildProgress({
  prepState,
  running,
  routeCount,
  message,
  unfinished,
}: {
  prepState: "idle" | "preparing" | "ready" | "error" | "warning";
  running: boolean;
  routeCount: number;
  message?: string;
  /** True when a partial route was returned (mark unreachable). */
  unfinished?: boolean;
}) {
  let phase = "";
  if (prepState === "preparing" || (running && prepState !== "error")) {
    phase = running ? "Routing…" : "Preparing route data…";
  } else if (prepState === "error") {
    phase = message || "Route failed";
  } else if (prepState === "warning" || unfinished) {
    phase = message || "Route unfinished — some marks unreachable";
  } else if (message && routeCount === 0 && !running) {
    phase = message;
  } else {
    return null;
  }

  const isError = prepState === "error" || (routeCount === 0 && !!message && !running);
  const isWarn = !isError && (prepState === "warning" || !!unfinished);

  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 600,
        color: isError ? "#f87171" : isWarn ? "#fbbf24" : "var(--text)",
        flexShrink: 0,
        maxWidth: 360,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      }}
      title={message || phase}
    >
      {phase}
    </div>
  );
}
