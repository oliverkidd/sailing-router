"use client";

/** Static legend for grey isobath lines on the nautical baseline. */
export default function DepthLegend({ visible = true }: { visible?: boolean }) {
  if (!visible) return null;

  const stops: { label: string; color: string; width: number }[] = [
    { label: "2–5 m", color: "#e2e8f0", width: 2.0 },
    { label: "10–20 m", color: "#94a3b8", width: 1.5 },
    { label: "50 m", color: "#64748b", width: 1.2 },
    { label: "100–200 m", color: "#475569", width: 0.9 },
  ];

  return (
    <div
      className="depth-legend"
      style={{
        position: "absolute",
        bottom: 70,
        left: 12,
        zIndex: 4,
        padding: "8px 10px",
        borderRadius: 8,
        background: "rgba(10, 22, 40, 0.88)",
        border: "1px solid rgba(148, 163, 184, 0.25)",
        backdropFilter: "blur(6px)",
        pointerEvents: "none",
        fontSize: 10,
        color: "#cbd5e1",
        lineHeight: 1.3,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 6, color: "#e2e8f0" }}>Depth</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {stops.map((s) => (
          <div key={s.label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span
              style={{
                width: 18,
                height: 0,
                borderTop: `${s.width}px solid ${s.color}`,
                opacity: 0.95,
              }}
            />
            <span>{s.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
