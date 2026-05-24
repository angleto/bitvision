"use client";

import { useTranslations } from "next-intl";

import type { DisplayMetadata } from "@/lib/api";

interface Props {
  metadata: DisplayMetadata;
}

/**
 * Per-series PET context panel for the viewer sidebar.
 *
 * Surfaces the SUV provenance the radiologist needs at a glance:
 * tracer, patient weight, scaling factor, units. The factor itself
 * is the multiplier the client applies to a pixel to get SUV bw —
 * exposing it makes the math auditable.
 *
 * If ``suv_factor_bw`` is null the panel still renders, but with a
 * warning explaining which tags are missing (server returns the
 * specific notes list). This is critical for clinical safety: the
 * reader must not assume "no SUV shown means SUV ~ 0".
 */
export default function PetInfoPanel({ metadata }: Props) {
  const tv = useTranslations("viewer");
  if (!metadata.is_pet) return null;
  const usable = metadata.suv_factor_bw != null;
  return (
    <section
      data-section="pet-info"
      style={{
        padding: "0.65rem 0.85rem",
        borderRadius: 6,
        marginTop: "0.75rem",
        background: usable ? "rgba(233,107,31,0.08)" : "rgba(180,40,40,0.08)",
        border: `1px solid ${usable ? "#e96b1f" : "#b91c1c"}`,
        color: "#e6ecf3",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 6,
        }}
      >
        <span
          style={{
            background: "#e96b1f",
            color: "#fff",
            fontSize: "0.65rem",
            fontWeight: 700,
            letterSpacing: "0.06em",
            padding: "1px 6px",
            borderRadius: 3,
          }}
        >
          PET
        </span>
        <span style={{ fontSize: "0.82rem", fontWeight: 600 }}>
          {metadata.radionuclide ?? tv("petTracerUnknown")}
        </span>
      </div>
      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          rowGap: 3,
          columnGap: 8,
          fontSize: "0.78rem",
          margin: 0,
        }}
      >
        <Row
          label={tv("petLabelWeight")}
          value={metadata.patient_weight_kg != null ? `${metadata.patient_weight_kg} kg` : "—"}
        />
        <Row label={tv("petLabelUnits")} value={metadata.units ?? "—"} />
        <Row
          label={tv("petLabelFactorBw")}
          value={
            metadata.suv_factor_bw != null
              ? metadata.suv_factor_bw.toExponential(3)
              : tv("petFactorNotComputable")
          }
        />
      </dl>
      {!usable && metadata.suv_notes.length > 0 && (
        <div
          style={{
            marginTop: 6,
            fontSize: "0.72rem",
            color: "#fca5a5",
          }}
        >
          {metadata.suv_notes.map((n) => (
            <div key={n}>• {n}</div>
          ))}
          <div style={{ marginTop: 4, fontStyle: "italic" }}>
            {tv("petSuvNotShownPrefix")} {metadata.units ?? tv("petUnitsRaw")}.
          </div>
        </div>
      )}
      {usable && (
        <div
          style={{
            marginTop: 6,
            fontSize: "0.72rem",
            color: "#94a3b8",
            lineHeight: 1.4,
          }}
        >
          {tv("petCursorHintPart1")} {tv("petCursorHintPart2")}{" "}
          <strong>{tv("petCursorHintVoiLabel")}</strong>.
        </div>
      )}
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt style={{ color: "#94a3b8" }}>{label}</dt>
      <dd
        style={{
          margin: 0,
          fontFamily: "ui-monospace, Menlo, monospace",
          color: "#e6ecf3",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {value}
      </dd>
    </>
  );
}
