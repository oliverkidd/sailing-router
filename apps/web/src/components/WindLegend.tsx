"use client";

import { useMemo } from "react";
import { WIND_SPEED_MAX_KN, windScale } from "@/lib/colors";

const TICKS = [0, 10, 20, 30, 40, 50, 60] as const;

export default function WindLegend({ visible }: { visible: boolean }) {
  const gradient = useMemo(() => {
    const stops: string[] = [];
    for (let kn = 0; kn <= WIND_SPEED_MAX_KN; kn++) {
      stops.push(`${windScale(kn).color} ${(kn / WIND_SPEED_MAX_KN) * 100}%`);
    }
    return `linear-gradient(to right, ${stops.join(", ")})`;
  }, []);

  if (!visible) return null;

  return (
    <div className="wind-legend" aria-label="Wind speed colour scale, 0 to 60 knots">
      <div className="wind-legend-bar" style={{ background: gradient }} />
      <div className="wind-legend-ticks">
        {TICKS.map((kn) => (
          <span
            key={kn}
            className="wind-legend-tick"
            style={{ left: `${(kn / WIND_SPEED_MAX_KN) * 100}%` }}
          >
            {kn === 0 || kn === 60 ? `${kn} knts` : kn}
          </span>
        ))}
      </div>
    </div>
  );
}
