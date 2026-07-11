"use client";

import { useMemo } from "react";
import { windyColor } from "@/lib/colors";

const TICKS = [0, 5, 10, 15, 20, 25, 30, 35, 40] as const;

export default function WindLegend({ visible }: { visible: boolean }) {
  const gradient = useMemo(() => {
    const stops: string[] = [];
    for (let kn = 0; kn <= 40; kn++) {
      stops.push(`${windyColor(kn)} ${(kn / 40) * 100}%`);
    }
    return `linear-gradient(to right, ${stops.join(", ")})`;
  }, []);

  if (!visible) return null;

  return (
    <div className="wind-legend" aria-label="Wind speed colour scale, 0 to 40 knots">
      <div className="wind-legend-bar" style={{ background: gradient }} />
      <div className="wind-legend-ticks">
        {TICKS.map((kn) => (
          <span
            key={kn}
            className="wind-legend-tick"
            style={{ left: `${(kn / 40) * 100}%` }}
          >
            {kn}
          </span>
        ))}
      </div>
      <div className="wind-legend-unit">knots</div>
    </div>
  );
}
