"use client";

import type { ClinicalNote } from "@/lib/api";

interface Props {
  /**
   * Either a ClinicalNote or a consultation-shaped object — the badge
   * only reads ``is_ai_generated``, ``model_id``, ``provider``. Loose
   * typing avoids importing the full Consultation interface here.
   */
  note: Pick<ClinicalNote, "is_ai_generated" | "model_id" | "provider">;
  size?: "sm" | "md";
}

/**
 * Visual marker for AI-authored artefacts.
 *
 * Liability requirement: a clinician must NEVER be able to mistake an
 * AI-generated artefact for human reasoning. ``author_kind=='agent'``
 * is the source of truth on the backend; this badge surfaces it with:
 *
 *   - a colored pill (warm yellow + warning icon) that reads as a
 *     "caution" mark, distinct from any of the neutral metadata
 *     badges already in the UI;
 *   - the model id (``Opus 4.7`` etc.) inline so the reader knows
 *     which engine produced it without an extra click;
 *   - a tooltip with the provider for completeness.
 *
 * The badge is intentionally bigger and more attention-grabbing than
 * surrounding meta — text alone (a small "agent" tag) is too easy to
 * miss when scanning a long note list.
 */
export default function AiAuthorBadge({ note, size = "sm" }: Props) {
  if (!note.is_ai_generated) return null;
  const label = note.model_id ? prettyModel(note.model_id) : "AI";
  const fontSize = size === "md" ? "0.82rem" : "0.7rem";
  const pad = size === "md" ? "0.25rem 0.6rem" : "0.15rem 0.5rem";
  return (
    <span
      title={
        note.provider
          ? `Generato da ${label} (${note.provider}). Contenuto NON autorizzato come parere umano.`
          : `Generato da ${label}. Contenuto NON autorizzato come parere umano.`
      }
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.3rem",
        padding: pad,
        borderRadius: 999,
        background: "var(--bv-warning-soft)",
        color: "var(--bv-warning)",
        border: "1px solid color-mix(in srgb, var(--bv-warning) 40%, transparent)",
        fontWeight: 600,
        fontSize,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      }}
    >
      <svg
        aria-hidden="true"
        width={size === "md" ? 14 : 11}
        height={size === "md" ? 14 : 11}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      AI · {label}
    </span>
  );
}

/**
 * Slug → display name. Best-effort; falls back to the raw model_id.
 * Keep this list small on purpose — a single source of truth is here
 * so badges across notes / consultations / detail pages stay
 * consistent.
 */
function prettyModel(id: string): string {
  const known: Record<string, string> = {
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4": "Opus 4",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "gpt-5": "GPT-5",
    "gpt-4o": "GPT-4o",
  };
  return known[id] ?? id;
}
