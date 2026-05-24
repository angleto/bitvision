# Measurement templates

The bitvision_phoenix viewer ships a small catalogue of measurement templates
that radiologists can load to drive structured, reproducible reporting. A
template groups several **slots**, each describing an expected measurement
(anatomical landmark, kind, unit, optional normal range). The picker UI lets
the user load a template, fill each slot with a numeric value (typed, or
copied from an on-canvas measurement) and surfaces validation warnings when a
value falls outside the published normal range.

## Files

- `frontend/src/lib/measurementTemplates.ts` - template catalogue and
  validation helpers. Pure data + pure functions, safe to import from anywhere.
- `frontend/src/components/MeasurementTemplatePicker.tsx` - client component
  that renders the catalogue selector + slot inputs and emits an `onSave`
  payload.

The picker is intentionally decoupled from `MPRViewport` and
`MeasurementOverlay`: it lives **next to** the viewer (typically in the right
side panel of the viewer page) and never mutates the overlay's state. This
keeps the existing tool-driven measurement flow untouched while adding an
orthogonal, template-driven capture path.

## Included templates

| Category          | Template id              | Slots |
| ----------------- | ------------------------ | ----- |
| Cardiac           | `cardiac-basic`          | LVEDD, LVESD, IVS, PW, LA, aortic root, RV |
| Chest             | `chest-basic`            | Cardiothoracic ratio, pulmonary trunk diameter |
| Abdomen           | `abdomen-basic`          | Liver (sagittal CC), spleen, portal vein, CBD, pancreatic duct |
| Spine             | `spine-basic`            | Vertebral body height, disc space, spinal canal AP |
| Oncology          | `oncology-recist-1-1`    | RECIST 1.1 target lesion long-axis |

Normal ranges are drawn from commonly cited adult references and are meant as
decision-support hints, not diagnostic thresholds. They can be tuned per
institution by editing the per-template `slots[].normal` entries.

## Slot schema

```ts
interface MeasurementSlot {
  id: string;            // stable within the template
  label: string;         // human-readable
  kind: "distance" | "angle" | "area" | "ratio" | "numeric";
  unit: "mm" | "cm" | "mm2" | "cm2" | "deg" | "ratio" | "none";
  normal?: { min?: number; max?: number; qualifier?: string };
  hint?: string;         // anatomical landmark, acquisition tip
  required?: boolean;    // marks slots that gate "template complete"
}
```

`MeasurementKind` is a **superset** of the tools implemented today in
`MeasurementOverlay` (`distance`, `angle`, `area`). Slots whose `kind` maps to
an existing overlay tool are prime candidates for auto-populate in a follow-up
unit; slots of kind `ratio` or `numeric` are filled manually for now.

## Validation

`validateSlotValue(slot, value)` returns one of:

- `ok` - empty optional slot, or value inside the normal range;
- `warning` - value outside `normal.min / normal.max` (yellow border);
- `error` - missing required value, non-finite number, or negative distance
  (red border).

The picker renders the message inline underneath each slot and shows a
`filled/required` completeness counter at the bottom.

## Integration hook

```tsx
import MeasurementTemplatePicker, {
  type FilledSlot,
} from "@/components/MeasurementTemplatePicker";

<MeasurementTemplatePicker
  initialTemplateId="cardiac-basic"
  onSave={(payload) => {
    // payload.templateId, payload.templateName
    // payload.slots: FilledSlot[]  (slotId, label, kind, unit, value)
    persistTemplateFilling(payload);
  }}
/>
```

`onSave` delivers every slot - including empty ones with `value: null` - so
downstream code can decide whether to persist partial fillings, round-trip to
the backend report, or attach to the current study.

## Extending the catalogue

1. Add a new `MeasurementTemplate` object in `measurementTemplates.ts`.
2. Push it into the `MEASUREMENT_TEMPLATES` array (order controls the
   dropdown order).
3. Re-use existing `MeasurementKind` / `MeasurementUnit` values whenever
   possible so the picker and future auto-populate logic can stay generic.

No changes to `MeasurementOverlay` or `MPRViewport` are needed when adding a
template.
