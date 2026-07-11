/** Format map click inspect popup HTML. */

export interface PointInspect {
  lat: number;
  lon: number;
  elevation_m?: number | null;
  depth_m?: number | null;
  on_land?: boolean | null;
  tws?: number | null;
  twd?: number | null;
}

const CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

export function compassLabel(deg: number): string {
  const i = Math.round((((deg % 360) + 360) % 360) / 22.5) % 16;
  return CARDINALS[i];
}

function fmt(n: number | null | undefined, digits = 1, unit = ""): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n.toFixed(digits)}${unit}`;
}

export function formatPointPopup(data: PointInspect): string {
  const onLand = data.on_land === true;
  const terrain =
    onLand
      ? `Elev: ${fmt(data.elevation_m, 0, " m")}`
      : data.depth_m != null
        ? `Depth: ${fmt(data.depth_m, 1, " m")}`
        : data.elevation_m != null && data.elevation_m < 0
          ? `Depth: ${fmt(-data.elevation_m, 1, " m")}`
          : `Elev: ${fmt(data.elevation_m, 0, " m")}`;

  const wind =
    data.tws != null
      ? `Wind: ${fmt(data.tws, 1, " kn")}${
          data.twd != null ? ` · ${fmt(data.twd, 0)}° ${compassLabel(data.twd)}` : ""
        }`
      : "Wind: —";

  return `<div style="font:12px/1.45 system-ui,sans-serif;color:#111;min-width:140px">
    <div style="opacity:.65;margin-bottom:4px">${data.lat.toFixed(3)}, ${data.lon.toFixed(3)}</div>
    <div><strong>${wind}</strong></div>
    <div>${terrain}</div>
  </div>`;
}
