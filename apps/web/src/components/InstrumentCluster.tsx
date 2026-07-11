"use client";

import type { RouteLeg } from "@/lib/types";

export default function InstrumentCluster({ leg }: { leg: RouteLeg }) {
  const twd = leg.twd ?? 0;
  const twa = leg.twa ?? 0;

  return (
    <div className="instrument-cluster map-instrument-cluster">
      <div className="gauge">
        <span className="gauge-label">TWS</span>
        <span className="gauge-value">{leg.tws}</span>
        <span className="gauge-unit">kn</span>
      </div>
      <div className="gauge gauge-dial">
        <span className="gauge-label">TWD</span>
        <div className="dial-wrap">
          <div className="dial-ring" />
          <div className="dial-arrow" style={{ transform: `rotate(${twd}deg)` }} />
          <span className="dial-readout">{twd}°</span>
        </div>
      </div>
      <div className="gauge">
        <span className="gauge-label">BSP</span>
        <span className="gauge-value">{leg.bsp}</span>
        <span className="gauge-unit">kn</span>
      </div>
      <div className="gauge">
        <span className="gauge-label">TWA</span>
        <span className="gauge-value">{twa}</span>
        <span className="gauge-unit">°</span>
      </div>
      <div className="gauge">
        <span className="gauge-label">SOG</span>
        <span className="gauge-value">{leg.sog}</span>
        <span className="gauge-unit">kn</span>
      </div>
    </div>
  );
}
