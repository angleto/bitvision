"use client";

import {
  MEASUREMENT_TEMPLATES,
  type MeasurementSlot,
  type MeasurementTemplate,
  formatNormalRange,
  validateSlotValue,
} from "@/lib/measurementTemplates";
import { useMemo, useState } from "react";

export interface FilledSlot {
  slotId: string;
  label: string;
  kind: MeasurementSlot["kind"];
  unit: MeasurementSlot["unit"];
  value: number | null;
}

interface Props {
  /** Optional callback fired when the user clicks "Save template" with all filled slots. */
  onSave?: (payload: {
    templateId: string;
    templateName: string;
    slots: FilledSlot[];
  }) => void;
  /** Pre-select a template by id (e.g. from a study-type heuristic). */
  initialTemplateId?: string;
}

/**
 * Lives next to the viewer (not inside MPRViewport/MeasurementOverlay) so the
 * tool-driven measurement flow stays untouched.
 */
export default function MeasurementTemplatePicker({ onSave, initialTemplateId }: Props) {
  const [templateId, setTemplateId] = useState<string>(
    initialTemplateId ?? MEASUREMENT_TEMPLATES[0]?.id ?? "",
  );
  const [values, setValues] = useState<Record<string, string>>({});

  const template: MeasurementTemplate | undefined = useMemo(
    () => MEASUREMENT_TEMPLATES.find((t) => t.id === templateId),
    [templateId],
  );

  const parseValue = (raw: string): number | null => {
    if (raw.trim() === "") return null;
    const n = Number.parseFloat(raw);
    return Number.isNaN(n) ? null : n;
  };

  const changeTemplate = (id: string) => {
    setTemplateId(id);
    setValues({});
  };

  const handleChange = (slotId: string, raw: string) => {
    setValues((prev) => ({ ...prev, [slotId]: raw }));
  };

  const handleSave = () => {
    if (!template || !onSave) return;
    const slots: FilledSlot[] = template.slots.map((s) => ({
      slotId: s.id,
      label: s.label,
      kind: s.kind,
      unit: s.unit,
      value: parseValue(values[s.id] ?? ""),
    }));
    onSave({
      templateId: template.id,
      templateName: template.name,
      slots,
    });
  };

  const handleReset = () => setValues({});

  if (!template) {
    return <div className="meta">No measurement templates available.</div>;
  }

  const completeness = (() => {
    const required = template.slots.filter((s) => s.required);
    if (required.length === 0) return null;
    const filled = required.filter((s) => parseValue(values[s.id] ?? "") !== null).length;
    return `${filled}/${required.length} required slots filled`;
  })();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
      <h2>Measurement templates</h2>

      <label className="meta" style={{ fontSize: "0.75rem" }}>
        Template
        <select
          value={templateId}
          onChange={(e) => changeTemplate(e.target.value)}
          style={{ display: "block", marginTop: "0.25rem", width: "100%" }}
        >
          {MEASUREMENT_TEMPLATES.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </label>

      <p className="meta" style={{ fontSize: "0.7rem", margin: 0 }}>
        {template.description}
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        {template.slots.map((slot) => {
          const raw = values[slot.id] ?? "";
          const parsed = parseValue(raw);
          const validation = validateSlotValue(slot, parsed);
          const normal = formatNormalRange(slot);

          const borderColor =
            validation.level === "error"
              ? "#f66"
              : validation.level === "warning"
                ? "#fb0"
                : "#444";
          const msgColor =
            validation.level === "error"
              ? "#f66"
              : validation.level === "warning"
                ? "#fb0"
                : "#8c8";

          return (
            <div
              key={slot.id}
              style={{
                border: `1px solid ${borderColor}`,
                borderRadius: 4,
                padding: "0.4rem 0.5rem",
                display: "flex",
                flexDirection: "column",
                gap: "0.2rem",
              }}
            >
              {/* The slot heading is associated with the input via
                  htmlFor → id; the previous structure used a <label>
                  with no nested control or htmlFor, which biome
                  flags (noLabelWithoutControl) and screen readers
                  can't follow. */}
              <label
                htmlFor={`mt-slot-${slot.id}`}
                style={{
                  fontSize: "0.78rem",
                  display: "flex",
                  justifyContent: "space-between",
                  gap: "0.4rem",
                }}
              >
                <span>
                  {slot.label}
                  {slot.required && (
                    <span style={{ color: "#f66" }} aria-label="required">
                      {" *"}
                    </span>
                  )}
                </span>
                <span className="meta" style={{ fontSize: "0.7rem" }}>
                  {slot.kind}
                  {slot.unit !== "none" ? ` · ${slot.unit}` : ""}
                </span>
              </label>

              <div style={{ display: "flex", gap: "0.3rem", alignItems: "center" }}>
                <input
                  id={`mt-slot-${slot.id}`}
                  type="number"
                  inputMode="decimal"
                  step="any"
                  value={raw}
                  placeholder={normal ? `Normal: ${normal}` : "Value"}
                  onChange={(e) => handleChange(slot.id, e.target.value)}
                  style={{ flex: 1, padding: "0.25rem 0.4rem", fontSize: "0.8rem" }}
                />
                {slot.unit !== "none" && (
                  <span className="meta" style={{ fontSize: "0.7rem" }}>
                    {slot.unit}
                  </span>
                )}
              </div>

              {slot.hint && (
                <p className="meta" style={{ fontSize: "0.68rem", margin: 0 }}>
                  {slot.hint}
                </p>
              )}

              {normal && (
                <p className="meta" style={{ fontSize: "0.68rem", margin: 0 }}>
                  Normal: {normal}
                  {slot.normal?.qualifier ? ` (${slot.normal.qualifier})` : ""}
                </p>
              )}

              {validation.message && (
                <p style={{ fontSize: "0.7rem", margin: 0, color: msgColor }}>
                  {validation.message}
                </p>
              )}
            </div>
          );
        })}
      </div>

      {completeness && (
        <p className="meta" style={{ fontSize: "0.7rem", margin: 0 }}>
          {completeness}
        </p>
      )}

      <div style={{ display: "flex", gap: "0.4rem" }}>
        <button type="button" className="viewer-btn" onClick={handleSave}>
          Save template
        </button>
        <button type="button" className="viewer-btn" onClick={handleReset}>
          Reset
        </button>
      </div>
    </div>
  );
}
