"use client";

import { useMemo } from "react";
import type { PolarCurve } from "@/lib/types";

// Simple SVG polar plot: TWA on a half-circle (0 up = into wind), one curve per
// TWS ring. Boat speed maps to radius.
export default function PolarDiagram({ curve }: { curve: PolarCurve | null }) {
  const size = 260;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 16;

  const maxSpeed = useMemo(() => {
    if (!curve) return 1;
    return Math.max(1, ...curve.speed.flat());
  }, [curve]);

  if (!curve) {
    return <div style={{ color: "var(--muted)", fontSize: 12 }}>No polar loaded.</div>;
  }

  const colors = ["#38bdf8", "#22d3ee", "#2dd4bf", "#4ade80", "#a3e635", "#facc15", "#fb923c"];

  const point = (twa: number, speed: number) => {
    // 0 deg TWA points up; angle sweeps clockwise to 180 at bottom.
    const rad = ((twa - 90) * Math.PI) / 180;
    const rr = (speed / maxSpeed) * r;
    return [cx + rr * Math.cos(rad), cy + rr * Math.sin(rad)];
  };

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Polar diagram">
      {/* radial grid */}
      {[0.25, 0.5, 0.75, 1].map((f) => (
        <circle key={f} cx={cx} cy={cy} r={r * f} fill="none" stroke="#22314f" strokeWidth={1} />
      ))}
      <line x1={cx} y1={cy - r} x2={cx} y2={cy + r} stroke="#2b3b5c" />
      <line x1={cx - r} y1={cy} x2={cx + r} y2={cy} stroke="#2b3b5c" />
      {curve.tws_axis.map((tws, ti) => {
        const pts = curve.twa_axis
          .map((twa, ai) => point(twa, curve.speed[ti][ai]))
          .map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`)
          .join(" ");
        return (
          <polyline
            key={tws}
            points={pts}
            fill="none"
            stroke={colors[ti % colors.length]}
            strokeWidth={1.5}
            opacity={0.9}
          />
        );
      })}
      <text x={cx} y={14} fill="#8fa3bf" fontSize={10} textAnchor="middle">
        into wind
      </text>
      <text x={cx} y={size - 4} fill="#8fa3bf" fontSize={10} textAnchor="middle">
        {maxSpeed.toFixed(1)} kn max
      </text>
    </svg>
  );
}
