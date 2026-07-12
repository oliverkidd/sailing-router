"use client";

export type MapLoadKind = "wind-initial" | "wind-hour" | "route";

/** In-map loading feedback — full panel for cold starts, light veil for hour refresh. */
export default function MapLoadOverlay({
  kind,
  label,
}: {
  kind: MapLoadKind | null;
  label: string;
}) {
  if (!kind) return null;

  const light = kind === "wind-hour";

  return (
    <div
      className={`map-load-overlay${light ? " map-load-overlay--light" : ""}`}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <div className="map-load-bar" aria-hidden />
      {!light && (
        <div className="map-load-card">
          <span className="map-load-spin" aria-hidden />
          <div className="map-load-copy">
            <div className="map-load-title">{label}</div>
            <div className="map-load-sub">Fetching forecast grid…</div>
          </div>
        </div>
      )}
      {light && (
        <div className="map-load-pill">
          <span className="map-load-spin map-load-spin--sm" aria-hidden />
          <span>{label}</span>
        </div>
      )}
    </div>
  );
}
