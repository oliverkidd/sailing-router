"use client";

import { useEffect, useState } from "react";
import {
  compareToRoute,
  formatAheadBehind,
  formatCrossTrack,
  formatStartsIn,
  type GpsFix,
  type RaceSnapshot,
} from "@/lib/liveRace";
import type { Course, RouteLeg } from "@/lib/types";

export function useLivePosition(enabled: boolean): {
  fix: GpsFix | null;
  error: string | null;
} {
  const [fix, setFix] = useState<GpsFix | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setError(null);
      return;
    }
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setError("This browser has no location.");
      return;
    }
    const id = navigator.geolocation.watchPosition(
      (pos) => {
        const heading = pos.coords.heading;
        const speed = pos.coords.speed;
        setError(null);
        setFix({
          position: [pos.coords.latitude, pos.coords.longitude],
          accuracyM: Number.isFinite(pos.coords.accuracy) ? pos.coords.accuracy : null,
          headingDeg: heading != null && Number.isFinite(heading) ? heading : null,
          speedKn: speed != null && speed >= 0 ? speed * 1.943844 : null,
          atUtc: new Date(pos.timestamp).toISOString(),
        });
      },
      (err) => {
        setError(err.message || "Location unavailable");
      },
      { enableHighAccuracy: true, maximumAge: 2000, timeout: 20000 },
    );
    return () => navigator.geolocation.clearWatch(id);
  }, [enabled]);

  return { fix, error };
}

export default function LiveRacePanel({
  enabled,
  onToggle,
  followNow,
  onFollowNow,
  fix,
  error,
  legs,
  departUtc,
  nowUtc,
  canReroute,
  onUseAsStart,
  onReroute,
}: {
  enabled: boolean;
  onToggle: () => void;
  followNow: boolean;
  onFollowNow: () => void;
  fix: GpsFix | null;
  error: string | null;
  legs: RouteLeg[] | null;
  departUtc: string;
  nowUtc: string;
  canReroute: boolean;
  onUseAsStart: () => void;
  onReroute: () => void;
}) {
  const snap: RaceSnapshot = compareToRoute(legs, fix?.position ?? null, nowUtc, departUtc);
  const showRace = enabled && !!legs?.length && !!fix;

  return (
    <div className={`live-race${enabled ? " live-race--on" : ""}`}>
      <div className="live-race-head">
        <button
          type="button"
          className={`live-race-toggle${enabled ? " live-race-toggle--on" : ""}`}
          onClick={onToggle}
          aria-pressed={enabled}
        >
          <span className="live-race-dot" aria-hidden />
          Live
        </button>
        {enabled && fix?.accuracyM != null && (
          <span className="live-race-meta">±{Math.round(fix.accuracyM)} m</span>
        )}
      </div>

      {enabled && error && <p className="live-race-error">{error}</p>}
      {enabled && !error && !fix && <p className="live-race-meta">Waiting for a GPS fix…</p>}

      {enabled && fix && (
        <div className="live-race-actions">
          <button type="button" className="live-race-btn" onClick={onUseAsStart}>
            Set start to my position
          </button>
        </div>
      )}

      {showRace && (
        <div className="live-race-stats">
          <div className="live-race-stat">
            <span>Vs plan</span>
            <strong>{snap.phase === "before-start" ? formatStartsIn(departUtc, nowUtc) : formatAheadBehind(snap.aheadBehindS)}</strong>
          </div>
          <div className="live-race-stat">
            <span>Off track</span>
            <strong>{formatCrossTrack(snap.along)}</strong>
          </div>
          {snap.phase === "finished" && <p className="live-race-meta">The planned boat has finished.</p>}
          {snap.departPassed && snap.phase !== "finished" && (
            <button
              type="button"
              className="live-race-btn live-race-btn--primary"
              onClick={onReroute}
              disabled={!canReroute}
            >
              Reroute from here
            </button>
          )}
          {!followNow && (
            <button type="button" className="live-race-btn" onClick={onFollowNow}>
              Show the plan at now
            </button>
          )}
          <p className="live-race-key">
            <span className="live-race-swatch live-race-swatch--you" /> You
            <span className="live-race-swatch live-race-swatch--plan" /> Plan at this time
          </p>
        </div>
      )}
    </div>
  );
}
