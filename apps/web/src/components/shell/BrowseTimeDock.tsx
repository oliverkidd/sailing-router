"use client";

import TimeScrubber from "@/components/TimeScrubber";
import type { ModelStatus } from "@/components/RouteBuildProgress";
import type { ModelPreset, RouteLeg } from "@/lib/types";

export default function BrowseTimeDock({
  showModels,
  models,
  selectedModels,
  activeModelId,
  onActiveModel,
  onSelectModel,
  overlayStatus,
  overlayTimeUtc,
  onTime,
  horizonStartUtc,
  horizonEndUtc,
  loading = false,
  routeActive = false,
  playUntilUtc = null,
  legs = null,
  distanceNm = 0,
}: {
  showModels: boolean;
  models: ModelPreset[];
  selectedModels: Set<string>;
  activeModelId: string;
  onActiveModel: (id: string) => void;
  onSelectModel?: (id: string) => void;
  overlayStatus: Record<string, ModelStatus>;
  overlayTimeUtc: string;
  onTime: (iso: string) => void;
  horizonStartUtc: string;
  horizonEndUtc: string;
  loading?: boolean;
  routeActive?: boolean;
  playUntilUtc?: string | null;
  legs?: RouteLeg[] | null;
  distanceNm?: number;
}) {
  /** Basic: all available models are choosable. Routing review: only selected. */
  const tabs = showModels
    ? models
    : models.filter((m) => selectedModels.has(m.id));
  const active = models.find((m) => m.id === activeModelId);

  return (
    <div
      className={[
        "browse-time-dock",
        showModels && tabs.length > 0 ? "browse-time-dock--with-models" : "",
        loading ? "browse-time-dock--loading" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {showModels && tabs.length > 0 && (
        <div className="browse-time-models" role="tablist" aria-label="Forecast model">
          {tabs.map((m) => {
            const isActive = m.id === activeModelId;
            const status = overlayStatus[m.id];
            return (
              <button
                key={m.id}
                type="button"
                role="tab"
                aria-selected={isActive}
                className={`browse-time-model${isActive ? " active" : ""}${
                  status === "error" ? " error" : ""
                }`}
                title={`${m.label} · ${m.resolution_deg}°`}
                onClick={() => {
                  onSelectModel?.(m.id);
                  onActiveModel(m.id);
                }}
              >
                {m.label}
                {status === "fetching" && (
                  <span className="browse-time-model-spin" aria-label="loading" />
                )}
              </button>
            );
          })}
        </div>
      )}

      {showModels && models.length === 0 && (
        <div className="browse-time-models-empty">
          Pan the map to load available forecasts
        </div>
      )}

      {loading && (
        <div className="browse-time-load-bar" aria-hidden>
          <div className="browse-time-load-bar-fill" />
        </div>
      )}

      <TimeScrubber
        overlayTimeUtc={overlayTimeUtc}
        onTime={onTime}
        horizonStartUtc={horizonStartUtc}
        horizonEndUtc={horizonEndUtc}
        loading={false}
        routeActive={routeActive}
        leg={null}
        legs={legs}
        playUntilUtc={playUntilUtc}
        distanceNm={distanceNm}
        subtitle={
          routeActive
            ? undefined
            : active
              ? `${active.label} · ${active.resolution_deg}°`
              : "Browse forecast"
        }
        showEmbeddedInstruments={false}
      />
    </div>
  );
}
