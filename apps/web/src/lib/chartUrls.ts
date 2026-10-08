/**
 * Chart archives live on a CDN in production (GitHub rejects >100 MB files).
 * Locally they are served from /geo/ on the same origin.
 *
 * Set NEXT_PUBLIC_CHART_BASE to an absolute origin or base URL, e.g.
 *   https://cdn.example.com
 *   https://cdn.example.com/charts
 * Trailing slashes are fine. Empty / unset → same-origin /geo/.
 */

const VERSION = "20261006a";

function chartBase(): string {
  const raw = (process.env.NEXT_PUBLIC_CHART_BASE ?? "").trim().replace(/\/$/, "");
  if (raw) return raw;
  if (typeof window !== "undefined") return window.location.origin;
  return "";
}

/** Absolute HTTP(S) URL for a chart archive (coastline.pmtiles / depth.pmtiles). */
export function chartArchiveUrl(name: "coastline.pmtiles" | "depth.pmtiles"): string {
  const base = chartBase();
  if (!base) return `/geo/${name}?v=${VERSION}`;
  if (base.includes("/geo")) return `${base}/${name}?v=${VERSION}`;
  return `${base}/geo/${name}?v=${VERSION}`;
}

/** pmtiles:// URL for MapLibre. */
export function chartPmtilesUrl(name: "coastline.pmtiles" | "depth.pmtiles"): string {
  return `pmtiles://${chartArchiveUrl(name)}`;
}

export const CHART_TILE_VERSION = VERSION;
