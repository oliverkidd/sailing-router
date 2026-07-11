"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { BoatConfig, PolarCurve } from "@/lib/types";
import PolarDiagram from "./PolarDiagram";

const DEFAULT_CONFIG: BoatConfig = {
  polar_id: "",
  tack_penalty_s: 30,
  gybe_penalty_s: 20,
  upwind_twa_deg: null,
  downwind_twa_deg: null,
  leeway_coeff: 0,
  leeway_max_deg: 8,
  polar_efficiency: 1,
};

function NumberRow({
  label,
  value,
  onChange,
  step = 1,
  placeholder,
}: {
  label: string;
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  step?: number;
  placeholder?: string;
}) {
  return (
    <label style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: "3px 0" }}>
      <span style={{ color: "var(--muted)", fontSize: 12 }}>{label}</span>
      <input
        type="number"
        step={step}
        value={value ?? ""}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        style={{
          width: 84,
          background: "#0b1220",
          border: "1px solid var(--border)",
          color: "var(--text)",
          borderRadius: 6,
          padding: "4px 6px",
          fontVariantNumeric: "tabular-nums",
        }}
      />
    </label>
  );
}

export default function BoatConfigPanel({
  config,
  onConfigChange,
  minDepthM,
  onMinDepthChange,
}: {
  config: BoatConfig;
  onConfigChange: (c: BoatConfig) => void;
  minDepthM: number;
  onMinDepthChange: (m: number) => void;
}) {
  const [curve, setCurve] = useState<PolarCurve | null>(null);
  const [status, setStatus] = useState<string>("");
  const fileRef = useRef<HTMLInputElement | null>(null);

  const set = (patch: Partial<BoatConfig>) => onConfigChange({ ...config, ...patch });

  useEffect(() => {
    if (config.polar_id) return;
    api
      .sampleBoat()
      .then((b) => {
        onConfigChange({ ...DEFAULT_CONFIG, ...b.boat });
        return api.polarCurve(b.polar_id);
      })
      .then(setCurve)
      .catch(() => setStatus("Backend offline — start the API to load a polar."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onUpload = async (file: File) => {
    setStatus("Uploading polar…");
    try {
      const b = await api.uploadBoat(file, config);
      onConfigChange({ ...config, ...b.boat });
      setCurve(await api.polarCurve(b.polar_id));
      setStatus(`Loaded ${b.polar_id}`);
    } catch (e) {
      setStatus(String(e));
    }
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "center", marginBottom: 8 }}>
        <PolarDiagram curve={curve} size={148} />
      </div>

      <button
        type="button"
        onClick={() => fileRef.current?.click()}
        style={{
          width: "100%",
          background: "var(--accent)",
          color: "#04231f",
          border: "none",
          borderRadius: 6,
          padding: "6px 8px",
          fontWeight: 600,
          cursor: "pointer",
          marginBottom: 8,
        }}
      >
        Upload polar (.pol or CSV)
      </button>
      <input
        ref={fileRef}
        type="file"
        accept=".pol,.csv,.txt"
        style={{ display: "none" }}
        onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
      />

      <NumberRow
        label="Safety depth (m)"
        value={minDepthM}
        step={0.5}
        onChange={(v) => onMinDepthChange(v ?? 0)}
      />
      <NumberRow label="Tack penalty (s)" value={config.tack_penalty_s} onChange={(v) => set({ tack_penalty_s: v ?? 0 })} />
      <NumberRow label="Gybe penalty (s)" value={config.gybe_penalty_s} onChange={(v) => set({ gybe_penalty_s: v ?? 0 })} />
      <NumberRow label="Upwind TWA°" value={config.upwind_twa_deg} placeholder="auto" onChange={(v) => set({ upwind_twa_deg: v })} />
      <NumberRow label="Downwind TWA°" value={config.downwind_twa_deg} placeholder="auto" onChange={(v) => set({ downwind_twa_deg: v })} />
      <NumberRow label="Leeway coeff" value={config.leeway_coeff} step={0.1} onChange={(v) => set({ leeway_coeff: v ?? 0 })} />
      <NumberRow label="Leeway max°" value={config.leeway_max_deg} onChange={(v) => set({ leeway_max_deg: v ?? 0 })} />
      <NumberRow label="Polar efficiency" value={config.polar_efficiency} step={0.05} onChange={(v) => set({ polar_efficiency: v ?? 1 })} />

      {status && <div style={{ color: "var(--muted)", fontSize: 11, marginTop: 6 }}>{status}</div>}
    </div>
  );
}
