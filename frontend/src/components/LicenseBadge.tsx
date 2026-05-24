"use client";

// Public-dataset license badge.
// Renders on study cards / study header when the study carries an
// SPDX license (i.e. it was imported by bvphoenix-public-import into
// the OpenData tier). Clicking the badge opens a small dialog with
// the full citation text + a link to the canonical license. Hidden
// entirely for private / user-uploaded studies (license_spdx null).

import { useTranslations } from "next-intl";
import { useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import type { Study } from "@/lib/api";

interface Props {
  study: Pick<
    Study,
    "license_spdx" | "license_url" | "citation_text" | "citation_required" | "source_collection"
  >;
  /**
   * "header" renders the full label ("Public Dataset · CC-BY · cite").
   * "compact" drops the prefix and shows just the license code, for
   * dense list rows.
   */
  variant?: "header" | "compact";
}

export default function LicenseBadge({ study, variant = "header" }: Props) {
  const t = useTranslations("studyLicense");
  const [open, setOpen] = useState(false);

  if (!study.license_spdx) return null;

  const label =
    variant === "compact"
      ? study.license_spdx
      : `${t("publicDataset")} · ${study.license_spdx}${study.citation_required ? ` · ${t("cite")}` : ""}`;

  return (
    <>
      <button
        type="button"
        className="badge badge--license"
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
                  {study.license_spdx} ↗
                </a>
              ) : (
                study.license_spdx
              )}
            </div>
          </div>

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
