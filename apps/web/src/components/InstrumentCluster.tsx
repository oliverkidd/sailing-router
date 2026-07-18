"use client";

import type { RouteLeg } from "@/lib/types";

function DialGauge({
  label,
  degrees,
}: {
  label: string;
  degrees: number;
}) {
  return (
    <div className="gauge gauge-dial">
      <span className="gauge-label">{label}</span>
      <div className="dial-wrap">
        <div className="dial-ring" />
        <div className="dial-arrow" style={{ transform: `rotate(${degrees}deg)` }} />
        <span className="dial-readout">{degrees}°</span>
      </div>
    </div>
  );
}

export default function InstrumentCluster({
  leg,
  embedded = false,
  distanceNm = 0,
}: {
  leg: RouteLeg;
  embedded?: boolean;
  distanceNm?: number;
}) {
  const twd = Math.round(leg.twd ?? 0);
  const twa = Math.round(Math.abs(leg.twa ?? 0));
  const tws = Number((leg.tws ?? 0).toFixed(1));
  const sog = Number((leg.sog ?? 0).toFixed(1));
  const dist = Number(distanceNm.toFixed(1));

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
      <DialGauge label="TWD" degrees={twd} />
      <DialGauge label="TWA" degrees={twa} />
      <div className="gauge">
        <span className="gauge-label">SOG</span>
        <span className="gauge-value">{sog}</span>
        <span className="gauge-unit">kn</span>
      </div>
      <div className="gauge">
        <span className="gauge-label">Dist</span>
        <span className="gauge-value">{dist}</span>
        <span className="gauge-unit">nm</span>
      </div>
    </div>
  );
}
