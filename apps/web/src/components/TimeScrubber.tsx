"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import InstrumentCluster from "@/components/InstrumentCluster";
import type { RouteLeg } from "@/lib/types";

const HOUR_MS = 3600 * 1000;
const DAY_MS = 24 * HOUR_MS;
const FIVE_MIN_MS = 5 * 60 * 1000;
/** Forecast time advanced per 1s of wall clock while playing at 1x (~20 min/s). */
const PLAY_RATE_MS_PER_SEC = 20 * 60 * 1000;

type PlaySpeed = 1 | 2 | 5;
const SPEED_CYCLE: PlaySpeed[] = [1, 2, 5];

function floorHourMs(iso: string): number {
  const d = new Date(iso);
  d.setUTCMinutes(0, 0, 0);
  return d.getTime();
}

function toIso(ms: number): string {
  return new Date(ms).toISOString();
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatDayHour(
  ms: number,
  withMinutes: boolean,
  withSeconds = false,
): { day: string; hour: string } {
  const d = new Date(ms);
  const day = `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = withMinutes || withSeconds ? String(d.getUTCMinutes()).padStart(2, "0") : "00";
  if (withSeconds) {
    const ss = String(d.getUTCSeconds()).padStart(2, "0");
    return { day, hour: `${hh}:${mm}:${ss} UTC` };
  }
  return { day, hour: `${hh}:${mm} UTC` };
}

const btnStyle: CSSProperties = {
  minWidth: 36,
  height: 32,
  padding: "0 8px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "transparent",
  color: "var(--text)",
  cursor: "pointer",
  fontSize: 11,
  fontWeight: 600,
  lineHeight: 1,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontVariantNumeric: "tabular-nums",
};

export default function TimeScrubber({
  overlayTimeUtc,
  onTime,
  horizonStartUtc,
  horizonEndUtc,
  loading = false,
  routeActive = false,
  leg = null,
  playUntilUtc = null,
  distanceNm = 0,
}: {
  overlayTimeUtc: string;
  onTime: (iso: string) => void;
  horizonStartUtc: string;
  horizonEndUtc: string;
  loading?: boolean;
  routeActive?: boolean;
  leg?: RouteLeg | null;
  /** When set, autoplay stops at this time (e.g. route ETA). */
  playUntilUtc?: string | null;
  distanceNm?: number;
}) {
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<PlaySpeed>(1);
  const [hasPlayed, setHasPlayed] = useState(false);
  const onTimeRef = useRef(onTime);
  onTimeRef.current = onTime;
  const curMsRef = useRef(0);
  const playEndRef = useRef(0);
  const speedRef = useRef(speed);
  speedRef.current = speed;

  const startMs = routeActive
    ? new Date(horizonStartUtc).getTime()
    : floorHourMs(horizonStartUtc);
  const endMs = routeActive
    ? new Date(horizonEndUtc).getTime()
    : floorHourMs(horizonEndUtc);
  const rawMs = new Date(overlayTimeUtc).getTime();
  const curMs = Math.min(
    endMs,
    Math.max(startMs, routeActive ? rawMs : floorHourMs(overlayTimeUtc)),
  );
  curMsRef.current = curMs;
  const { day, hour } = formatDayHour(curMs, routeActive, playing);
  const atStart = curMs <= startMs;
  const atEnd = curMs >= endMs;
  const playEndMs =
    playUntilUtc != null ? Math.min(endMs, new Date(playUntilUtc).getTime()) : endMs;
  playEndRef.current = playEndMs;
  const atPlayEnd = curMs >= playEndMs;

  useEffect(() => {
    if (!routeActive) {
      setPlaying(false);
      setHasPlayed(false);
      setSpeed(1);
    }
  }, [routeActive]);

  useEffect(() => {
    if (atPlayEnd) setPlaying(false);
  }, [atPlayEnd]);

  useEffect(() => {
    if (!playing || !routeActive) return;

    let raf = 0;
    let last = performance.now();
    let playhead = curMsRef.current;

    const tick = (now: number) => {
      const dt = Math.min(0.064, (now - last) / 1000);
      last = now;
      playhead = Math.min(
        playEndRef.current,
        playhead + PLAY_RATE_MS_PER_SEC * speedRef.current * dt,
      );
      onTimeRef.current(toIso(playhead));
      if (playhead >= playEndRef.current) {
        setPlaying(false);
        return;
      }
      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, routeActive]);

  const step = (deltaMs: number) => {
    if (playing) setPlaying(false);
    const next = Math.min(endMs, Math.max(startMs, curMs + deltaMs));
    onTime(toIso(next));
  };

  const stepRef = useRef(step);
  stepRef.current = step;

  useEffect(() => {
    if (!routeActive) return;

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || target?.isContentEditable) {
        return;
      }
      e.preventDefault();
      const delta = FIVE_MIN_MS * speedRef.current;
      stepRef.current(e.key === "ArrowLeft" ? -delta : delta);
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [routeActive]);

  const cycleSpeed = () => {
    setSpeed((s) => {
      const i = SPEED_CYCLE.indexOf(s);
      return SPEED_CYCLE[(i + 1) % SPEED_CYCLE.length];
    });
  };

  const togglePlay = () => {
    setPlaying((p) => {
      const next = !p;
      if (next) setHasPlayed(true);
      return next;
    });
  };

  return (
    <div
      className={[
        "time-nav",
        loading ? "time-nav--loading" : "",
        routeActive ? "time-nav--route" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      role="group"
      aria-label="Forecast time"
      aria-busy={loading || undefined}
    >
      <div className="time-nav-row">
        <button
          type="button"
          style={{ ...btnStyle, opacity: atStart ? 0.35 : 1 }}
          onClick={() => step(-DAY_MS)}
          disabled={atStart}
          aria-label="Previous day"
          title="−24 hours"
        >
          - 24h
        </button>
        <button
          type="button"
          style={{ ...btnStyle, opacity: atStart ? 0.35 : 1 }}
          onClick={() => step(-HOUR_MS)}
          disabled={atStart}
          aria-label="Previous hour"
          title="−1 hour"
        >
          - 1h
        </button>

        <div className="time-nav-label">
          <div className="time-nav-datetime">
            {loading && <span className="time-nav-spin" aria-hidden />}
            {loading ? "Loading forecast…" : `${hour} · ${day}`}
          </div>
          {routeActive && (
            <div className="time-nav-play-row">
              <button
                type="button"
                className="time-nav-play"
                onClick={togglePlay}
                disabled={atPlayEnd && !playing}
                aria-label={playing ? "Pause route playback" : "Play route playback"}
                title={playing ? "Pause" : "Play"}
              >
                {playing ? (
                  <span className="time-nav-pause-icon" aria-hidden />
                ) : (
                  <span className="time-nav-play-icon" aria-hidden />
                )}
              </button>
              {hasPlayed && (
                <button
                  type="button"
                  className="time-nav-speed"
                  onClick={cycleSpeed}
                  aria-label={`Playback speed ${speed}x`}
                  title="Cycle playback speed"
                >
                  {speed}x
                </button>
              )}
            </div>
          )}
        </div>

        <button
          type="button"
          style={{ ...btnStyle, opacity: atEnd ? 0.35 : 1 }}
          onClick={() => step(HOUR_MS)}
          disabled={atEnd}
          aria-label="Next hour"
          title="+1 hour"
        >
          + 1h
        </button>
        <button
          type="button"
          style={{ ...btnStyle, opacity: atEnd ? 0.35 : 1 }}
          onClick={() => step(DAY_MS)}
          disabled={atEnd}
          aria-label="Next day"
          title="+24 hours"
        >
          + 24h
        </button>
      </div>

      {leg && <InstrumentCluster leg={leg} embedded distanceNm={distanceNm} />}
    </div>
  );
}
