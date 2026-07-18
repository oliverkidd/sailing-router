/** Format map click inspect popup HTML — HUD-style gauges matching InstrumentCluster. */

export interface PointInspect {
  lat: number;
  lon: number;
  elevation_m?: number | null;
  depth_m?: number | null;
  depth_lt_m?: number | null;
  on_land?: boolean | null;
  dem_available?: boolean | null;
  tws?: number | null;
  twd?: number | null;
}

const CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

export function compassLabel(deg: number): string {
  const i = Math.round((((deg % 360) + 360) % 360) / 22.5) % 16;
  return CARDINALS[i];
}

function fmtNum(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function terrainGauge(data: PointInspect): { label: string; value: string; unit: string } {
  const onLand = data.on_land === true;
  const elev = data.elevation_m;
  const elevOk = elev != null && !Number.isNaN(elev);

  // DEM-first: positive DEM height wins even when on_land is null/false
  // (prepare-cache miss or OSM/DEM shore mismatch). Sea DEM is often 0.0 — not Elev.
  if (elevOk && elev > 0) {
    return { label: "Elev", value: fmtNum(elev, 0), unit: "m" };
  }

  if (onLand) {
    if (!elevOk) {
      const hint = data.dem_available === false || data.dem_available == null ? " (no DEM)" : "";
      return { label: "Elev", value: `—${hint}`, unit: "m" };
    }
    return { label: "Elev", value: fmtNum(elev, 0), unit: "m" };
  }
  if (data.depth_m != null) {
    return { label: "Depth", value: fmtNum(data.depth_m, 1), unit: "m" };
  }
  if (data.depth_lt_m != null) {
    return { label: "Depth", value: `<${data.depth_lt_m}`, unit: "m" };
  }
  if (elevOk && elev < 0) {
    return { label: "Depth", value: fmtNum(-elev, 1), unit: "m" };
  }
  return { label: "Elev", value: fmtNum(elev, 0), unit: "m" };
}

export function formatPointPopup(data: PointInspect): string {
  const terrain = terrainGauge(data);
  const twd = data.twd != null && !Number.isNaN(data.twd) ? ((data.twd % 360) + 360) % 360 : null;
  const tws = data.tws != null && !Number.isNaN(data.tws) ? data.tws : null;
  const twdLabel = twd != null ? `${Math.round(twd)}` : "—";
  const compass = twd != null ? compassLabel(twd) : "";

  return `<div class="map-click-hud">
    <div class="map-click-hud-coords">${data.lat.toFixed(3)}, ${data.lon.toFixed(3)}</div>
    <div class="instrument-cluster map-click-hud-gauges">
      <div class="gauge">
        <span class="gauge-label">TWS</span>
        <span class="gauge-value">${tws != null ? tws.toFixed(1) : "—"}</span>
        <span class="gauge-unit">kn</span>
      </div>
      <div class="gauge gauge-dial">
        <span class="gauge-label">TWD</span>
        <div class="dial-wrap">
          <div class="dial-ring"></div>
          ${twd != null ? `<div class="dial-arrow" style="transform:rotate(${twd}deg)"></div>` : ""}
          <span class="dial-readout">${twdLabel}°</span>
        </div>
        ${compass ? `<span class="gauge-unit">${compass}</span>` : `<span class="gauge-unit"> </span>`}
      </div>
      <div class="gauge">
        <span class="gauge-label">${terrain.label}</span>
        <span class="gauge-value">${terrain.value}</span>
        <span class="gauge-unit">${terrain.unit}</span>
      </div>
    </div>
  </div>`;
}
