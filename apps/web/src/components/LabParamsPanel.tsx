"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { fieldIsModified, type LabValues } from "@/lib/labRuns";
import type { TuningField, TuningSchemaResponse } from "@/lib/types";

const DEFAULT_OPEN = new Set(["Resolution", "Boat", "Maneuver penalties"]);
const FLASH_MS = 1400;

/**
 * The router charges same-tack course changes as a fraction of the boat's tack
 * penalty; gybe_penalty_s is only ever used for an actual gybe. Spelling that
 * out is the whole point of the derived rows.
 */
const GYBE_NOTE =
  "Gybe penalty is not part of this: the router bills same-tack course changes against tack_penalty_s, and gybe_penalty_s only when the boat actually gybes.";

function parseFieldValue(field: TuningField, raw: string): number | null {
  if (raw === "") return field.nullable ? null : field.default;
  const n = Number(raw);
  if (!Number.isFinite(n)) return field.default;
  if (field.step >= 1 && Number.isInteger(field.step)) return Math.round(n);
  return n;
}

function trimNum(n: number, digits = 2): string {
  if (!Number.isFinite(n)) return "—";
  return String(Number(n.toFixed(digits)));
}

/** RouterParamsOverride is all-numeric, so an on/off knob arrives as 0..1 by 1. */
function isToggle(field: TuningField): boolean {
  return field.min === 0 && field.max === 1 && field.step === 1;
}

function InfoTip({ text, label }: { text: string; label: string }) {
  return (
    <span className="lab-info" tabIndex={0} role="note" aria-label={`${label}: ${text}`}>
      ⓘ
      <span className="lab-tip">{text}</span>
    </span>
  );
}

function FieldRow({
  field,
  value,
  flashed,
  onChange,
  onReset,
}: {
  field: TuningField;
  value: number | null;
  flashed: boolean;
  onChange: (v: number | null) => void;
  onReset: () => void;
}) {
  const dirty = fieldIsModified(field, { [field.name]: value });
  return (
    <div
      className={`lab-param-row${dirty ? " modified" : ""}${flashed ? " flash" : ""}`}
    >
      <label htmlFor={`lab-${field.name}`}>
        {field.label}
        <InfoTip text={field.description} label={field.label} />
      </label>
      {isToggle(field) ? (
        <input
          id={`lab-${field.name}`}
          type="checkbox"
          className="lab-param-check"
          checked={(value ?? 0) > 0}
          onChange={(e) => onChange(e.target.checked ? 1 : 0)}
        />
      ) : (
        <input
          id={`lab-${field.name}`}
          type="number"
          step={field.step}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          value={value ?? ""}
          placeholder={field.default == null ? "" : String(field.default)}
          onChange={(e) => onChange(parseFieldValue(field, e.target.value))}
        />
      )}
      <button
        type="button"
        className="lab-icon-btn"
        disabled={!dirty}
        onClick={onReset}
        title="Reset to default"
      >
        ↺
      </button>
    </div>
  );
}

/** Computed at runtime by the router, so it is shown but never sent. */
function DerivedRow({
  label,
  seconds,
  derivation,
  onJump,
}: {
  label: string;
  seconds: number;
  derivation: string;
  onJump: () => void;
}) {
  return (
    <div className="lab-param-row derived">
      <label>{label}</label>
      <span className="lab-derived-value">{trimNum(seconds, 1)}</span>
      <button
        type="button"
        className="lab-icon-btn lab-link-btn"
        onClick={onJump}
        aria-label={`${label}: ${derivation}`}
      >
        🔗
        <span className="lab-tip">{derivation}</span>
      </button>
    </div>
  );
}

export default function LabParamsPanel({
  schema,
  values,
  onChange,
  onResetAll,
}: {
  schema: TuningSchemaResponse;
  values: LabValues;
  onChange: (next: LabValues) => void;
  onResetAll: () => void;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [openCats, setOpenCats] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(schema.categories.map((c) => [c, DEFAULT_OPEN.has(c)])),
  );
  const [flash, setFlash] = useState<{ name: string; at: number } | null>(null);

  const byName = useMemo(
    () => new Map(schema.fields.map((f) => [f.name, f])),
    [schema.fields],
  );

  const byCat = useMemo(() => {
    const map = new Map<string, TuningField[]>();
    for (const cat of schema.categories) map.set(cat, []);
    for (const f of schema.fields) {
      const list = map.get(f.category) ?? [];
      list.push(f);
      map.set(f.category, list);
    }
    return schema.categories
      .filter((c) => (map.get(c) ?? []).length > 0)
      .map((c) => ({ category: c, fields: map.get(c) ?? [] }));
  }, [schema]);

  useEffect(() => {
    if (!flash) return;
    document.getElementById(`lab-${flash.name}`)?.scrollIntoView({
      block: "center",
      behavior: "smooth",
    });
    const id = setTimeout(() => setFlash(null), FLASH_MS);
    return () => clearTimeout(id);
  }, [flash]);

  const numOf = (name: string): number => {
    const v = values[name];
    if (v != null && Number.isFinite(v)) return v;
    return byName.get(name)?.default ?? 0;
  };

  const jumpTo = (name: string) => {
    const cat = byName.get(name)?.category;
    if (cat) setOpenCats((c) => ({ ...c, [cat]: true }));
    setFlash({ name, at: Date.now() });
  };

  /** Seconds the router will actually charge, from the fractions above. */
  const derivedTurnRows = useMemo(() => {
    const tack = numOf("tack_penalty_s");
    const scale = numOf("course_change_tws_scale_kn");
    const moderate = numOf("course_change_moderate_deg");
    const cap = (frac: number) => Math.min(tack, frac * tack);
    return [
      {
        key: "mild",
        label: "Mild turn (s)",
        seconds: cap(numOf("course_change_mild_frac")),
        derivation: `${trimNum(numOf("course_change_mild_frac"))} × tack_penalty_s (Boat) = ${trimNum(cap(numOf("course_change_mild_frac")), 1)} s. Charged for a same-tack turn wider than ${trimNum(moderate)}° while TWS is below ${trimNum(scale)} kn. ${GYBE_NOTE}`,
      },
      {
        key: "heavy-small",
        label: `Heavy small turn @ ${trimNum(scale)} kn (s)`,
        seconds: cap(numOf("course_change_heavy_frac_small")),
        derivation: `${trimNum(numOf("course_change_heavy_frac_small"))} × tack_penalty_s (Boat) = ${trimNum(cap(numOf("course_change_heavy_frac_small")), 1)} s at exactly ${trimNum(scale)} kn. Charged for a turn up to ${trimNum(moderate)}°, and it scales up by TWS ÷ ${trimNum(scale)} kn above that, capped at the full tack penalty. ${GYBE_NOTE}`,
      },
      {
        key: "heavy-large",
        label: `Heavy large turn @ ${trimNum(scale)} kn (s)`,
        seconds: cap(numOf("course_change_heavy_frac_large")),
        derivation: `${trimNum(numOf("course_change_heavy_frac_large"))} × tack_penalty_s (Boat) = ${trimNum(cap(numOf("course_change_heavy_frac_large")), 1)} s at exactly ${trimNum(scale)} kn. Charged for a turn wider than ${trimNum(moderate)}°, and it scales up by TWS ÷ ${trimNum(scale)} kn above that, capped at the full tack penalty. ${GYBE_NOTE}`,
      },
    ];
    // numOf reads `values`; recompute whenever any of them move.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values, byName]);

  const setField = (name: string, v: number | null) => onChange({ ...values, [name]: v });

  const resetCategory = (fields: TuningField[]) => {
    const next = { ...values };
    for (const f of fields) next[f.name] = f.default ?? null;
    onChange(next);
  };

  const onExport = () => {
    const blob = new Blob([JSON.stringify(values, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "routing-lab-params.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  const onImportFile = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text()) as Record<string, unknown>;
      if (!parsed || typeof parsed !== "object") throw new Error("not an object");
      const next = { ...values };
      const known = new Set(schema.fields.map((f) => f.name));
      for (const [k, v] of Object.entries(parsed)) {
        if (!known.has(k)) continue;
        if (v === null) next[k] = null;
        else if (typeof v === "number" && Number.isFinite(v)) next[k] = v;
      }
      onChange(next);
    } catch (err) {
      alert(`Could not import JSON: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <div className="lab-params">
      <div className="lab-params-toolbar">
        <button type="button" className="rail-section-action" onClick={onResetAll}>
          Reset all
        </button>
        <button type="button" className="rail-section-action" onClick={onExport}>
          Export JSON
        </button>
        <button type="button" className="rail-section-action" onClick={() => fileRef.current?.click()}>
          Import JSON
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) void onImportFile(f);
          }}
        />
      </div>
      {byCat.map(({ category, fields }) => {
        const dirtyN = fields.filter((f) => fieldIsModified(f, values)).length;
        return (
          <details
            key={category}
            className="lab-cat"
            open={openCats[category] ?? false}
            onToggle={(e) => {
              // Read the event synchronously: React runs the updater later, by
              // which time currentTarget has been released.
              const open = e.currentTarget.open;
              setOpenCats((c) => ({ ...c, [category]: open }));
            }}
          >
            <summary>
              <span>{category}</span>
              {dirtyN > 0 && <span className="lab-cat-dirty">{dirtyN} changed</span>}
              <button
                type="button"
                className="lab-cat-reset"
                onClick={(e) => {
                  e.preventDefault();
                  resetCategory(fields);
                }}
              >
                Reset
              </button>
            </summary>
            {fields.map((f) => (
              <FieldRow
                key={f.name}
                field={f}
                value={values[f.name] === undefined ? (f.default ?? null) : values[f.name]}
                flashed={flash?.name === f.name}
                onChange={(v) => setField(f.name, v)}
                onReset={() => setField(f.name, f.default ?? null)}
              />
            ))}
            {category === "Maneuver penalties" && (
              <>
                <p className="lab-derived-head">Derived from Boat · tack penalty</p>
                {derivedTurnRows.map((row) => (
                  <DerivedRow
                    key={row.key}
                    label={row.label}
                    seconds={row.seconds}
                    derivation={row.derivation}
                    onJump={() => jumpTo("tack_penalty_s")}
                  />
                ))}
              </>
            )}
          </details>
        );
      })}
    </div>
  );
}
