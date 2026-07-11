"use client";

import { useEffect, useRef } from "react";
import type { ModelRoute } from "@/lib/types";

export default function RoutePlayback({
  routes,
  routeIndex,
  legIndex,
  onLegIndex,
  playing,
  onPlaying,
}: {
  routes: ModelRoute[];
  routeIndex: number;
  legIndex: number;
  onLegIndex: (i: number) => void;
  playing: boolean;
  onPlaying: (p: boolean) => void;
}) {
  const route = routes[routeIndex];
  const legs = route?.legs ?? [];
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!playing || legs.length === 0) {
      if (timer.current) clearInterval(timer.current);
      return;
    }
    timer.current = setInterval(() => {
      onLegIndex(Math.min(legIndex + 1, legs.length - 1));
    }, 500);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [playing, legIndex, legs.length, onLegIndex]);

  if (legs.length === 0) return null;

  const leg = legs[legIndex];

  return (
    <div className="playback-bar">
      <button type="button" onClick={() => onPlaying(!playing)} aria-label={playing ? "Pause" : "Play"}>
        {playing ? "⏸" : "▶"}
      </button>
      <input
        type="range"
        min={0}
        max={Math.max(0, legs.length - 1)}
        value={legIndex}
        onChange={(e) => onLegIndex(Number(e.target.value))}
        style={{ flex: 1 }}
      />
      <span style={{ fontSize: 11, color: "var(--muted)", minWidth: 120 }}>
        {leg ? new Date(leg.t_utc).toISOString().slice(0, 16).replace("T", " ") : "—"} UTC
      </span>
      <span style={{ fontSize: 11, color: "var(--muted)" }}>
        {legIndex + 1}/{legs.length}
      </span>
    </div>
  );
}
