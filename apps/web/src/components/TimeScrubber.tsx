"use client";

import type { CSSProperties } from "react";

const HOUR_MS = 3600 * 1000;
const DAY_MS = 24 * HOUR_MS;

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

function formatDayHour(ms: number): { day: string; hour: string } {
  const d = new Date(ms);
  const day = `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  const hour = `${String(d.getUTCHours()).padStart(2, "0")}:00 UTC`;
  return { day, hour };
}

const btnStyle: CSSProperties = {
  width: 32,
  height: 32,
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "transparent",
  color: "var(--text)",
  cursor: "pointer",
  fontSize: 16,
  lineHeight: 1,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

export default function TimeScrubber({
  overlayTimeUtc,
  onTime,
  horizonStartUtc,
  horizonEndUtc,
}: {
  overlayTimeUtc: string;
  onTime: (iso: string) => void;
  horizonStartUtc: string;
  horizonEndUtc: string;
}) {
  const startMs = floorHourMs(horizonStartUtc);
  const endMs = floorHourMs(horizonEndUtc);
  const curMs = Math.min(endMs, Math.max(startMs, floorHourMs(overlayTimeUtc)));
  const { day, hour } = formatDayHour(curMs);
  const atStart = curMs <= startMs;
  const atEnd = curMs >= endMs;

  const step = (deltaMs: number) => {
    const next = Math.min(endMs, Math.max(startMs, curMs + deltaMs));
    onTime(toIso(next));
  };

  return (
    <div className="time-nav" role="group" aria-label="Forecast time">
      <button
        type="button"
        style={{ ...btnStyle, opacity: atStart ? 0.35 : 1 }}
        onClick={() => step(-DAY_MS)}
        disabled={atStart}
        aria-label="Previous day"
        title="−1 day"
      >
        «
      </button>
      <button
        type="button"
        style={{ ...btnStyle, opacity: atStart ? 0.35 : 1 }}
        onClick={() => step(-HOUR_MS)}
        disabled={atStart}
        aria-label="Previous hour"
        title="−1 hour"
      >
        ‹
      </button>

      <div className="time-nav-label">
        <div className="time-nav-hour">{hour}</div>
        <div className="time-nav-day">{day}</div>
      </div>

      <button
        type="button"
        style={{ ...btnStyle, opacity: atEnd ? 0.35 : 1 }}
        onClick={() => step(HOUR_MS)}
        disabled={atEnd}
        aria-label="Next hour"
        title="+1 hour"
      >
        ›
      </button>
      <button
        type="button"
        style={{ ...btnStyle, opacity: atEnd ? 0.35 : 1 }}
        onClick={() => step(DAY_MS)}
        disabled={atEnd}
        aria-label="Next day"
        title="+1 day"
      >
        »
      </button>
    </div>
  );
}
