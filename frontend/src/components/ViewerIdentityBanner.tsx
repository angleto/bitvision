"use client";

// Always-on patient + study identity strip for the viewer chrome.
//
// Correct-patient / correct-study verification is the #1 wrong-patient
// safety control in a reading room: the radiologist must confirm WHO and
// WHICH study is on screen without leaving the viewport. This strip lives
// in the viewer chrome (not burned into the pixel canvas, so screenshots
// and exports stay free of demographics) and is always visible.
//
// ``patient`` may be null when the reader is not authorised to see the
// demographics (public datasets, restricted shares) — the strip then
// degrades to study-only identity rather than leaking nothing useful.

import type { Patient, Study } from "@/lib/api";

function computeAge(birth: string): number | null {
  const d = new Date(birth);
  if (Number.isNaN(d.getTime())) return null;
  const now = new Date();
  let age = now.getFullYear() - d.getFullYear();
  const m = now.getMonth() - d.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < d.getDate())) age -= 1;
  return age >= 0 && age < 200 ? age : null;
}

function formatDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso; // show raw if unparseable
  return d.toLocaleDateString();
}

function sexLabel(sex: string | null | undefined): string | null {
  if (!sex) return null;
  const s = sex.trim().toUpperCase();
  if (s.startsWith("M")) return "M";
  if (s.startsWith("F")) return "F";
  return s.slice(0, 1) || null;
}

const SEP = "·";

export interface ViewerIdentityBannerProps {
  patient: Patient | null;
  study: Study | null;
  /** Optional accent colour (compare/multi panes colour-code per study). */
  accentColor?: string;
  /** Compact one-line variant for per-pane rendering. */
  compact?: boolean;
}

export default function ViewerIdentityBanner({
  patient,
  study,
  accentColor,
  compact,
}: ViewerIdentityBannerProps) {
  if (!patient && !study) return null;

  const name = patient?.display_name?.trim() || "Unknown patient";
  const mrn = patient?.external_id?.trim() || null;
  const dob = formatDate(patient?.birth_date);
  const age = patient?.birth_date ? computeAge(patient.birth_date) : null;
  const sex = sexLabel(patient?.sex);

  const studyDate = formatDate(study?.study_date);
  const studyDesc = study?.study_description?.trim() || null;
  const modality = study?.modalities?.length ? study.modalities.join("/") : null;

  const patientBits = [
    mrn ? `ID ${mrn}` : null,
    dob ? (age != null ? `${dob} (${age}y)` : dob) : null,
    sex,
  ].filter(Boolean);
  const studyBits = [studyDesc, studyDate, modality].filter(Boolean);

  return (
    <div
      data-testid="viewer-identity-banner"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        flexWrap: "wrap",
        padding: compact ? "1px 6px" : "3px 10px",
        fontFamily: "ui-monospace, monospace",
        fontSize: compact ? "0.68rem" : "0.78rem",
        lineHeight: 1.3,
        color: "#e6ecf3",
        background: "rgba(8, 12, 20, 0.92)",
        borderBottom: `2px solid ${accentColor ?? "#334155"}`,
        textShadow: "0 1px 2px rgba(0,0,0,0.7)",
        minWidth: 0,
      }}
    >
      <span
        style={{
          fontWeight: 700,
          color: "#ffffff",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
          maxWidth: compact ? "12rem" : "22rem",
        }}
        title={name}
      >
        {name}
      </span>
      {patientBits.length > 0 && (
        <span style={{ color: "#94a3b8", whiteSpace: "nowrap" }}>
          {patientBits.join(` ${SEP} `)}
        </span>
      )}
      {studyBits.length > 0 && (
        <>
          <span style={{ color: "#475569" }} aria-hidden>
            |
          </span>
          <span
            style={{
              color: "#cbd5e1",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              minWidth: 0,
            }}
            title={studyBits.join(` ${SEP} `)}
          >
            {studyBits.join(` ${SEP} `)}
          </span>
        </>
      )}
    </div>
  );
}
