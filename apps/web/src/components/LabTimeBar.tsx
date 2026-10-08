"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;
const SCRUB_STEP_MIN = 5;
const PLAY_FRAME_MS = 140;
const PLAY_MIN_PER_FRAME = 5;

export function hourLabelUtc(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getUTCHours()).padStart(2, "0")}Z`;
}

export function clockLabelUtc(iso: string): string {
  const d = new Date(iso);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}Z`;
}

/**
 * One compact clock shared by every course card, so the same GFS hour is on
 * screen in all six maps at once. Not the full TimeScrubber instrument cluster.
 */
export default function LabTimeBar({
  startUtc,
  endUtc,
  valueUtc,
  playing,
  onChange,
  onPlayingChange,
  note,
}: {
  startUtc: string;
  endUtc: string;
  valueUtc: string;
  playing: boolean;
  onChange: (iso: string) => void;
  onPlayingChange: (playing: boolean) => void;
  note?: string;
}) {
  const t0 = new Date(startUtc).getTime();
  const t1 = Math.max(t0 + HOUR_MS, new Date(endUtc).getTime());
  const span = t1 - t0;
  const value = Math.min(t1, Math.max(t0, new Date(valueUtc).getTime()));
  const totalMin = Math.round(span / MINUTE_MS);
  const curMin = Math.round((value - t0) / MINUTE_MS);

  const valueRef = useRef(value);
  valueRef.current = value;

  const emitMinutes = useCallback(
    (min: number) => {
      const clamped = Math.min(totalMin, Math.max(0, min));
      onChange(new Date(t0 + clamped * MINUTE_MS).toISOString());
    },
    [onChange, t0, totalMin],
  );

  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => {
      const next = valueRef.current + PLAY_MIN_PER_FRAME * MINUTE_MS;
      if (next >= t1) {
        onChange(new Date(t1).toISOString());
        onPlayingChange(false);
        return;
      }
      onChange(new Date(next).toISOString());
    }, PLAY_FRAME_MS);
    return () => clearInterval(id);
  }, [playing, t1, onChange, onPlayingChange]);

  const ticks = useMemo(() => {
    const first = Math.ceil(t0 / HOUR_MS) * HOUR_MS;
    const out: { ms: number; pct: number; label: string }[] = [];
    for (let ms = first; ms <= t1; ms += HOUR_MS) {
      out.push({
        ms,
        pct: ((ms - t0) / span) * 100,
        label: hourLabelUtc(new Date(ms).toISOString()),
      });
    }
    return out;
  }, [t0, t1, span]);

  const stepHour = (dir: number) => {
    const base = Math.floor(value / HOUR_MS) * HOUR_MS;
    const target = dir > 0 ? base + HOUR_MS : value - base > MINUTE_MS ? base : base - HOUR_MS;
    emitMinutes(Math.round((target - t0) / MINUTE_MS));
  };

  return (
    <div className="lab-timebar">
      <button
        type="button"
        className="lab-icon-btn"
        onClick={() => onPlayingChange(!playing)}
        title={playing ? "Pause" : "Play through the forecast"}
      >
        {playing ? "❚❚" : "▶"}
      </button>
      <button type="button" className="lab-icon-btn" onClick={() => stepHour(-1)} title="Previous hour">
        ‹
      </button>
      <div className="lab-timebar-track">
        <input
          type="range"
          min={0}
          max={totalMin}
          step={SCRUB_STEP_MIN}
          value={curMin}
          aria-label="Forecast time"
          onChange={(e) => emitMinutes(Number(e.target.value))}
        />
        <div className="lab-timebar-ticks" aria-hidden="true">
          {ticks.map((tick) => (
            <span key={tick.ms} className="lab-timebar-tick" style={{ left: `${tick.pct}%` }}>
              {tick.label}
            </span>
          ))}
        </div>
      </div>
      <button type="button" className="lab-icon-btn" onClick={() => stepHour(1)} title="Next hour">
        ›
      </button>
      <span className="lab-timebar-clock">{clockLabelUtc(new Date(value).toISOString())}</span>
      <button type="button" className="rail-section-action" onClick={() => emitMinutes(0)}>
        Depart
      </button>
      {note && <span className="lab-muted">{note}</span>}
    </div>
  );
}
