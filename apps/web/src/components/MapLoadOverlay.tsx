"use client";

import RouteProgress from "./RouteProgress";
import type { RouteProgressStep } from "@/lib/types";

export type MapLoadKind = "wind-initial" | "wind-hour" | "route";

/** In-map loading feedback — full panel for cold starts, light veil for hour refresh. */
export default function MapLoadOverlay({
  kind,
  label,
  routeSteps,
  routeModelIds = [],
  routeModelLabels = {},
  windSteps,
  routeWarnings,
  routeError,
  retryableModelIds = [],
  onRetry,
  onRetryModel,
  onEdit,
}: {
  kind: MapLoadKind | null;
  label: string;
  routeSteps?: RouteProgressStep[];
  routeModelIds?: string[];
  routeModelLabels?: Record<string, string>;
  /** Cold wind browse checklist (SSE stages). */
  windSteps?: RouteProgressStep[];
  routeWarnings?: string[];
  routeError?: boolean;
  retryableModelIds?: string[];
  onRetry?: () => void;
  onRetryModel?: (modelId: string) => void;
  onEdit?: () => void;
}) {
  if (!kind) return null;

  const light = kind === "wind-hour";
  const showRouteProgress = kind === "route" && routeSteps && routeSteps.length > 0;
  const showWindProgress =
    kind === "wind-initial" && windSteps && windSteps.length > 0;

  return (
    <div
      className={`map-load-overlay${light ? " map-load-overlay--light" : ""}`}
      role="status"
      aria-live="polite"
      aria-busy={!routeError}
    >
      <div className="map-load-bar" aria-hidden />
      {showRouteProgress && (
        <div className="map-load-card map-load-card--progress">
          <RouteProgress
            steps={routeSteps}
            modelIds={routeModelIds}
            modelLabels={routeModelLabels}
            warnings={routeWarnings}
            showActions={routeError}
            retryableModelIds={retryableModelIds}
            onRetry={routeError ? onRetry : undefined}
            onRetryModel={onRetryModel}
            onEdit={routeError ? onEdit : undefined}
          />
        </div>
      )}
      {showWindProgress && (
        <div className="map-load-card map-load-card--progress">
          <div className="map-load-copy" style={{ marginBottom: "0.5rem" }}>
            <div className="map-load-title">{label}</div>
          </div>
          <RouteProgress steps={windSteps} modelIds={[]} />
        </div>
      )}
      {!light && !showRouteProgress && !showWindProgress && (
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
