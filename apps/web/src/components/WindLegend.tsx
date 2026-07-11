"use client";

import { useMemo } from "react";
import { windyColor } from "@/lib/colors";

const TICKS = [0, 10, 20, 30, 40];

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
          <span key={kn}>{kn}</span>
        ))}
      </div>
      <div className="wind-legend-label">kn</div>
    </div>
  );
}
