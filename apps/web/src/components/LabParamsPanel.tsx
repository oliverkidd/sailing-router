"use client";

import { useMemo, useRef } from "react";
import { fieldIsModified, type LabValues } from "@/lib/labRuns";
import type { TuningField, TuningSchemaResponse } from "@/lib/types";

function parseFieldValue(field: TuningField, raw: string): number | null {
  if (raw === "") return field.nullable ? null : field.default;
  const n = Number(raw);
  if (!Number.isFinite(n)) return field.default;
  if (field.step >= 1 && Number.isInteger(field.step)) return Math.round(n);
  return n;
}

function FieldRow({
  field,
  value,
  onChange,
  onReset,
}: {
  field: TuningField;
  value: number | null;
  onChange: (v: number | null) => void;
  onReset: () => void;
}) {
  const dirty = fieldIsModified(field, { [field.name]: value });
  return (
    <div className={`lab-param-row${dirty ? " modified" : ""}`} title={field.description}>
      <label htmlFor={`lab-${field.name}`}>{field.label}</label>
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
        const open = category === "Resolution" || category === "Boat" || category === "Maneuver penalties";
        return (
          <details key={category} className="lab-cat" defaultOpen={open}>
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
                onChange={(v) => setField(f.name, v)}
                onReset={() => setField(f.name, f.default ?? null)}
              />
            ))}
          </details>
        );
      })}
    </div>
  );
}
