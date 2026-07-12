"use client";

import type { RouteLeg } from "@/lib/types";

export default function InstrumentCluster({
  leg,
  embedded = false,
}: {
  leg: RouteLeg;
  embedded?: boolean;
}) {
  const twd = Math.round(leg.twd ?? 0);
  const twa = Math.round(leg.twa ?? 0);
  const tws = Number((leg.tws ?? 0).toFixed(1));
  const bsp = Number((leg.bsp ?? 0).toFixed(1));
  const sog = Number((leg.sog ?? 0).toFixed(1));

  return (
    <div
      className={
        embedded
          ? "instrument-cluster time-nav-instruments"
          : "instrument-cluster map-instrument-cluster"
      }
    >
      <div className="gauge">
        <span className="gauge-label">TWS</span>
        <span className="gauge-value">{tws}</span>
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
        <span className="gauge-value">{bsp}</span>
        <span className="gauge-unit">kn</span>
      </div>
      <div className="gauge">
        <span className="gauge-label">TWA</span>
        <span className="gauge-value">{twa}</span>
        <span className="gauge-unit">°</span>
      </div>
      <div className="gauge">
        <span className="gauge-label">SOG</span>
        <span className="gauge-value">{sog}</span>
        <span className="gauge-unit">kn</span>
      </div>
    </div>
  );
}
