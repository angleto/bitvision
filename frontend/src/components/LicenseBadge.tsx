"use client";

// Public-dataset license badge.
// Renders on study cards / study header, and on pathology slide cards /
// the deep-zoom viewer, when the entity carries an SPDX license (i.e.
// it was imported by a bvphoenix-public-import path into the OpenData
// tier). Clicking the badge opens a small dialog with the full citation
// text + a link to the canonical license. Hidden entirely for private /
// user-uploaded entities (license_spdx null).
//
// Label logic:
//   - CC0 / public-domain  → "Public Dataset · CC0 / public domain"
//     (no "cite" wording — CC0 waives attribution — but the dialog
//     stays so the source collection is still discoverable).
//   - commercialUseAllowed === false (CC-BY-NC-*) → "Non-commercial ·
//     educational use only" (+ " · cite" when citation_required),
//     rendered with the ``badge--license-nc`` modifier so it reads as
//     a usage restriction, not a generic provenance chip.
//   - otherwise (CC-BY)    → "Public Dataset · {spdx} · cite".

import { useTranslations } from "next-intl";
import { useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import type { Study } from "@/lib/api";

/**
 * The license-bearing fields the badge needs. Both ``Study`` and
 * ``PathologySlide`` structurally satisfy this, so the badge takes the
 * minimal shape rather than a specific entity type — it renders for any
 * OpenData entity that carries a license.
 */
export interface LicenseInfo {
  license_spdx?: string | null;
  license_url?: string | null;
  citation_text?: string | null;
  citation_required?: boolean;
  source_collection?: string | null;
  /** False for CC-BY-NC-* (non-commercial / educational reuse only). */
  commercial_use_allowed?: boolean;
}

interface Props {
  /** The license-bearing entity (a study, a pathology slide, or any
   *  object exposing the license fields). */
  study: Pick<
    Study,
    | "license_spdx"
    | "license_url"
    | "citation_text"
    | "citation_required"
    | "source_collection"
    | "commercial_use_allowed"
  > &
    LicenseInfo;
  /**
   * "header" renders the full label ("Public Dataset · CC-BY · cite").
   * "compact" drops the prefix and shows just the license code, for
   * dense list rows.
   */
  variant?: "header" | "compact";
  /**
   * Explicit override for the commercial-use flag. When omitted the
   * value on ``study.commercial_use_allowed`` is used. Lets callers
   * that only have the boolean (not the full object) still drive the
   * non-commercial treatment.
   */
  commercialUseAllowed?: boolean;
}

export default function LicenseBadge({ study, variant = "header", commercialUseAllowed }: Props) {
  const t = useTranslations("studyLicense");
  const [open, setOpen] = useState(false);

  if (!study.license_spdx) return null;

  const spdx = study.license_spdx;
  const isCc0 = spdx.toUpperCase().startsWith("CC0");
  const commercialOk = commercialUseAllowed ?? study.commercial_use_allowed ?? true;
  // CC0 waives attribution; for everything else honour citation_required.
  const isNonCommercial = !isCc0 && commercialOk === false;

  // Modifier class so the non-commercial chip reads as a restriction
  // (amber) rather than a generic provenance chip (indigo).
  const modifierClass = isNonCommercial ? "badge--license-nc" : "";

  let label: string;
  if (variant === "compact") {
    label = spdx;
  } else if (isCc0) {
    label = `${t("publicDataset")} · ${t("publicDomain")}`;
  } else if (isNonCommercial) {
    label = `${t("nonCommercial")} · ${t("educationalUseOnly")}${
      study.citation_required ? ` · ${t("cite")}` : ""
    }`;
  } else {
    label = `${t("publicDataset")} · ${spdx}${study.citation_required ? ` · ${t("cite")}` : ""}`;
  }

  return (
    <>
      <button
        type="button"
        className={`badge badge--license${modifierClass ? ` ${modifierClass}` : ""}`}
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-label={t("openCitation")}
        title={t("openCitation")}
      >
        {label}
      </button>
      <NativeDialog
        open={open}
        onClose={() => setOpen(false)}
        ariaLabel={t("dialogAriaLabel")}
        className="bv-dialog"
      >
        <div
          style={{
            background: "var(--color-surface, #fff)",
            borderRadius: 8,
            padding: "1.25rem",
            maxWidth: 560,
            display: "flex",
            flexDirection: "column",
            gap: "0.85rem",
          }}
        >
          <h2 style={{ margin: 0, fontSize: "1.1rem" }}>{t("dialogTitle")}</h2>

          {study.source_collection && (
            <div>
              <div style={{ fontSize: "0.75rem", color: "var(--bv-muted)" }}>
                {t("collectionLabel")}
              </div>
              <div style={{ fontFamily: "var(--bv-mono, monospace)" }}>
                {study.source_collection}
              </div>
            </div>
          )}

          <div>
            <div style={{ fontSize: "0.75rem", color: "var(--bv-muted)" }}>{t("licenseLabel")}</div>
            <div>
              {study.license_url ? (
                <a
                  href={study.license_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: "var(--bv-link, #2563eb)" }}
                >
                  {spdx} ↗
                </a>
              ) : (
                spdx
              )}
            </div>
          </div>

          {isNonCommercial && (
            <div
              style={{
                fontSize: "0.85rem",
                padding: "0.5rem 0.7rem",
                background: "var(--bv-warn-soft, #fef3c7)",
                borderLeft: "3px solid var(--bv-warn, #d97706)",
                borderRadius: 4,
              }}
            >
              {t("nonCommercialNote")}
            </div>
          )}

          {study.citation_text && (
            <div>
              <div style={{ fontSize: "0.75rem", color: "var(--bv-muted)" }}>
                {study.citation_required ? t("citationRequiredLabel") : t("citationOptionalLabel")}
              </div>
              <blockquote
                style={{
                  margin: 0,
                  padding: "0.6rem 0.8rem",
                  background: "var(--bv-info-soft, #eef2ff)",
                  borderLeft: "3px solid var(--bv-info, #6366f1)",
                  borderRadius: 4,
                  fontSize: "0.88rem",
                  lineHeight: 1.45,
                  whiteSpace: "pre-wrap",
                }}
              >
                {study.citation_text}
              </blockquote>
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button type="button" className="bv-btn" onClick={() => setOpen(false)}>
              {t("close")}
            </button>
          </div>
        </div>
      </NativeDialog>
    </>
  );
}
