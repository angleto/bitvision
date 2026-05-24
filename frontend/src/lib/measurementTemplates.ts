/**
 * Measurement template catalogue. See `docs/measurement-templates.md`.
 *
 * `MeasurementKind` is intentionally a superset of the tools currently
 * implemented in `MeasurementOverlay` (`distance`, `angle`, `area`), so slots
 * of kind `ratio` / `numeric` remain valid even though they must be filled
 * manually until matching overlay tools exist.
 */

export type MeasurementKind = "distance" | "angle" | "area" | "ratio" | "numeric";

export type MeasurementUnit = "mm" | "cm" | "mm2" | "cm2" | "deg" | "ratio" | "none";

export interface NormalRange {
  /** Inclusive lower bound. Omit for one-sided ranges. */
  min?: number;
  /** Inclusive upper bound. Omit for one-sided ranges. */
  max?: number;
  /** Optional free-text qualifier (e.g. "adult male", "RECIST 1.1 target"). */
  qualifier?: string;
}

export interface MeasurementSlot {
  /** Stable slot id, unique within a template. */
  id: string;
  /** Human-readable label shown in the picker. */
  label: string;
  /** Expected measurement kind (hints the tool + validation). */
  kind: MeasurementKind;
  /** Display unit. */
  unit: MeasurementUnit;
  /** Optional normal range for flagging out-of-range values. */
  normal?: NormalRange;
  /** Optional short guidance (anatomical landmark, acquisition tip). */
  hint?: string;
  /** Whether the slot must be filled to consider the template complete. */
  required?: boolean;
}

export type TemplateCategory = "cardiac" | "chest" | "abdomen" | "spine" | "oncology";

export interface MeasurementTemplate {
  id: string;
  name: string;
  category: TemplateCategory;
  description: string;
  slots: MeasurementSlot[];
}

/* ------------------------------------------------------------------ */
/* Cardiac                                                            */
/* ------------------------------------------------------------------ */

const cardiac: MeasurementTemplate = {
  id: "cardiac-basic",
  name: "Cardiac (basic chamber + wall)",
  category: "cardiac",
  description:
    "Left ventricle, septum/posterior wall, left atrium, aortic root and right ventricle. Adult reference ranges.",
  slots: [
    {
      id: "lvedd",
      label: "LV end-diastolic diameter (LVEDD)",
      kind: "distance",
      unit: "mm",
      normal: { min: 42, max: 58, qualifier: "adult" },
      hint: "Parasternal long-axis / short-axis at end-diastole, tip of mitral leaflets.",
      required: true,
    },
    {
      id: "lvesd",
      label: "LV end-systolic diameter (LVESD)",
      kind: "distance",
      unit: "mm",
      normal: { min: 25, max: 40, qualifier: "adult" },
      hint: "Same level as LVEDD, measured at end-systole.",
      required: true,
    },
    {
      id: "ivs",
      label: "Interventricular septum thickness (IVS)",
      kind: "distance",
      unit: "mm",
      normal: { min: 6, max: 11, qualifier: "adult, end-diastole" },
    },
    {
      id: "pw",
      label: "Posterior wall thickness (PW)",
      kind: "distance",
      unit: "mm",
      normal: { min: 6, max: 11, qualifier: "adult, end-diastole" },
    },
    {
      id: "la",
      label: "Left atrium diameter (LA)",
      kind: "distance",
      unit: "mm",
      normal: { min: 27, max: 40, qualifier: "AP diameter, adult" },
    },
    {
      id: "aortic-root",
      label: "Aortic root diameter",
      kind: "distance",
      unit: "mm",
      normal: { min: 20, max: 37, qualifier: "sinuses of Valsalva, adult" },
    },
    {
      id: "rv",
      label: "Right ventricle diameter (RV)",
      kind: "distance",
      unit: "mm",
      normal: { min: 25, max: 41, qualifier: "basal, adult" },
    },
  ],
};

/* ------------------------------------------------------------------ */
/* Chest                                                              */
/* ------------------------------------------------------------------ */

const chest: MeasurementTemplate = {
  id: "chest-basic",
  name: "Chest (CXR / CT screening)",
  category: "chest",
  description: "Cardiothoracic ratio and pulmonary trunk caliber.",
  slots: [
    {
      id: "ctr",
      label: "Cardiothoracic ratio (CTR)",
      kind: "ratio",
      unit: "ratio",
      normal: { max: 0.5, qualifier: "upright PA radiograph" },
      hint: "Max transverse cardiac diameter / max inner thoracic diameter.",
      required: true,
    },
    {
      id: "pulmonary-trunk",
      label: "Pulmonary trunk diameter",
      kind: "distance",
      unit: "mm",
      normal: { max: 29, qualifier: "axial CT at bifurcation level" },
      hint: "Measured perpendicular to long axis, just distal to valve.",
    },
  ],
};

/* ------------------------------------------------------------------ */
/* Abdomen                                                            */
/* ------------------------------------------------------------------ */

const abdomen: MeasurementTemplate = {
  id: "abdomen-basic",
  name: "Abdomen (solid organs + biliary/pancreatic ducts)",
  category: "abdomen",
  description: "Liver CC diameter, spleen, portal vein, CBD and pancreatic duct.",
  slots: [
    {
      id: "liver-cc",
      label: "Liver sagittal (cranio-caudal) diameter",
      kind: "distance",
      unit: "mm",
      normal: { max: 160, qualifier: "mid-clavicular line, adult" },
      hint: "Measured on sagittal plane through mid-clavicular line.",
      required: true,
    },
    {
      id: "spleen",
      label: "Spleen long-axis diameter",
      kind: "distance",
      unit: "mm",
      normal: { max: 120, qualifier: "adult" },
    },
    {
      id: "portal-vein",
      label: "Portal vein diameter",
      kind: "distance",
      unit: "mm",
      normal: { max: 13, qualifier: "main portal vein, adult" },
    },
    {
      id: "cbd",
      label: "Common bile duct (CBD) diameter",
      kind: "distance",
      unit: "mm",
      normal: { max: 7, qualifier: "adult, intact gallbladder" },
      hint: "Inner-to-inner wall at porta hepatis.",
    },
    {
      id: "pancreatic-duct",
      label: "Pancreatic duct diameter",
      kind: "distance",
      unit: "mm",
      normal: { max: 3, qualifier: "body of pancreas, adult" },
    },
  ],
};

/* ------------------------------------------------------------------ */
/* Spine                                                              */
/* ------------------------------------------------------------------ */

const spine: MeasurementTemplate = {
  id: "spine-basic",
  name: "Spine (single level)",
  category: "spine",
  description: "Vertebral body height, disc space and spinal canal AP diameter at one level.",
  slots: [
    {
      id: "vertebral-body-height",
      label: "Vertebral body height",
      kind: "distance",
      unit: "mm",
      normal: { min: 18, max: 30, qualifier: "lumbar, adult" },
      hint: "Anterior, middle and/or posterior height; record anterior by default.",
      required: true,
    },
    {
      id: "disc-space-height",
      label: "Disc space height",
      kind: "distance",
      unit: "mm",
      normal: { min: 7, max: 13, qualifier: "lumbar, adult" },
    },
    {
      id: "spinal-canal-ap",
      label: "Spinal canal AP diameter",
      kind: "distance",
      unit: "mm",
      normal: { min: 12, qualifier: "lumbar; <12 mm suggests stenosis" },
    },
  ],
};

/* ------------------------------------------------------------------ */
/* Oncology follow-up (RECIST 1.1)                                    */
/* ------------------------------------------------------------------ */

const oncology: MeasurementTemplate = {
  id: "oncology-recist-1-1",
  name: "Oncology follow-up (RECIST 1.1)",
  category: "oncology",
  description:
    "Target lesion long-axis per RECIST 1.1. Clone this slot once per target lesion (max 5 total, max 2 per organ).",
  slots: [
    {
      id: "target-lesion-long-axis",
      label: "Target lesion - long axis",
      kind: "distance",
      unit: "mm",
      normal: { min: 10, qualifier: "RECIST 1.1 target threshold (>=10 mm)" },
      hint: "Longest diameter in the plane of acquisition. Lymph nodes use short axis (>=15 mm).",
      required: true,
    },
  ],
};

/* ------------------------------------------------------------------ */
/* Catalogue                                                          */
/* ------------------------------------------------------------------ */

export const MEASUREMENT_TEMPLATES: MeasurementTemplate[] = [
  cardiac,
  chest,
  abdomen,
  spine,
  oncology,
];

export function getTemplateById(id: string): MeasurementTemplate | undefined {
  return MEASUREMENT_TEMPLATES.find((t) => t.id === id);
}

export function getTemplatesByCategory(category: TemplateCategory): MeasurementTemplate[] {
  return MEASUREMENT_TEMPLATES.filter((t) => t.category === category);
}

/* ------------------------------------------------------------------ */
/* Validation                                                         */
/* ------------------------------------------------------------------ */

export type SlotValidationLevel = "ok" | "warning" | "error";

export interface SlotValidation {
  level: SlotValidationLevel;
  message: string;
}

/**
 * Validates a numeric value against a slot's expected kind + normal range.
 * - Returns `error` for missing required values or non-finite numbers.
 * - Returns `warning` for values outside the normal range.
 * - Returns `ok` otherwise.
 */
export function validateSlotValue(
  slot: MeasurementSlot,
  value: number | null | undefined,
): SlotValidation {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return slot.required
      ? { level: "error", message: "Required slot is empty." }
      : { level: "ok", message: "" };
  }
  if (!Number.isFinite(value)) {
    return { level: "error", message: "Value is not a finite number." };
  }
  if (value < 0 && slot.kind !== "ratio") {
    return { level: "error", message: "Value must be non-negative." };
  }
  const n = slot.normal;
  if (n) {
    if (n.min !== undefined && value < n.min) {
      return {
        level: "warning",
        message: `Below normal range (min ${n.min} ${slot.unit}).`,
      };
    }
    if (n.max !== undefined && value > n.max) {
      return {
        level: "warning",
        message: `Above normal range (max ${n.max} ${slot.unit}).`,
      };
    }
  }
  return { level: "ok", message: "Within expected range." };
}

export function formatNormalRange(slot: MeasurementSlot): string {
  const n = slot.normal;
  if (!n) return "";
  const unit = slot.unit === "none" ? "" : ` ${slot.unit}`;
  if (n.min !== undefined && n.max !== undefined) return `${n.min}-${n.max}${unit}`;
  if (n.min !== undefined) return `>= ${n.min}${unit}`;
  if (n.max !== undefined) return `<= ${n.max}${unit}`;
  return "";
}
