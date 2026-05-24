"use client";

// Header strip rendered right under the breadcrumb when the user
// navigates *into* a folder that carries clinical context — the
// folder's own ``description``, ``narrative_md`` or ``clinical_date``.
// At the patient root (no folder context) this component renders
// nothing.
//
// Why a dedicated header: the same fields are surfaced inside the
// hover tooltip on the folder grid card, but once you click into the
// folder the card is gone, leaving the user without that context.
// Without this strip, the agent's clinical commentary stays invisible
// for as long as the user is browsing children, which defeats the
// purpose of writing the commentary in the first place.

import { useTranslations } from "next-intl";
import { useState } from "react";
import ReactMarkdown from "react-markdown";

import type { TreeNode } from "@/lib/api";

interface Props {
  folder: TreeNode;
}

export default function CurrentFolderHeader({ folder }: Props) {
  const t = useTranslations("fascicolo.v3.currentFolder");
  const [narrativeOpen, setNarrativeOpen] = useState(false);

  const hasDescription = !!(folder.description && folder.description.trim().length > 0);
  const hasNarrative = !!(folder.narrative_md && folder.narrative_md.trim().length > 0);
  const hasClinicalDate = !!folder.clinical_date;

  if (!hasDescription && !hasNarrative && !hasClinicalDate) return null;

  return (
    <section
      data-current-folder-header=""
      style={{
        marginTop: "0.5rem",
        marginBottom: "0.75rem",
        padding: "0.7rem 0.9rem",
        background: "var(--bv-card-bg)",
        border: "1px solid var(--bv-card-border)",
        borderRadius: "var(--bv-r-md)",
      }}
    >
      {hasClinicalDate && (
        <div
          className="meta"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.35rem",
            fontSize: "0.78rem",
            marginBottom: hasDescription || hasNarrative ? "0.35rem" : 0,
          }}
        >
          <span style={{ textTransform: "uppercase", letterSpacing: "0.03em" }}>
            {t("clinicalDateLabel")}
          </span>
          <strong>{folder.clinical_date}</strong>
        </div>
      )}

      {hasDescription && (
        <div style={{ fontSize: "0.92rem", lineHeight: 1.5 }}>
          <ReactMarkdown
            components={{
              p: ({ children }) => <p style={{ margin: "0 0 0.4rem" }}>{children}</p>,
              ul: ({ children }) => (
                <ul style={{ margin: "0 0 0.4rem", paddingLeft: "1.25em" }}>{children}</ul>
              ),
              ol: ({ children }) => (
                <ol style={{ margin: "0 0 0.4rem", paddingLeft: "1.25em" }}>{children}</ol>
              ),
              a: ({ href, children }) => (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ),
            }}
          >
            {folder.description ?? ""}
          </ReactMarkdown>
        </div>
      )}

      {hasNarrative && (
        <div style={{ marginTop: hasDescription ? "0.5rem" : 0 }}>
          <button
            type="button"
            className="ghost"
            onClick={() => setNarrativeOpen((v) => !v)}
            aria-expanded={narrativeOpen}
            aria-controls="bv-current-folder-narrative"
            style={{ fontSize: "0.78rem", padding: "0.2rem 0.55rem" }}
          >
            {narrativeOpen ? `▴ ${t("narrativeHide")}` : `▾ ${t("narrativeShow")}`}
          </button>
          {narrativeOpen && (
            <div
              id="bv-current-folder-narrative"
              style={{
                marginTop: "0.5rem",
                padding: "0.55rem 0.75rem",
                background: "var(--bv-bg, transparent)",
                border: "1px solid var(--bv-card-border)",
                borderRadius: "var(--bv-r-sm)",
                fontSize: "0.9rem",
                lineHeight: 1.55,
              }}
            >
              <ReactMarkdown
                components={{
                  p: ({ children }) => <p style={{ margin: "0 0 0.5rem" }}>{children}</p>,
                  ul: ({ children }) => (
                    <ul style={{ margin: "0 0 0.5rem", paddingLeft: "1.25em" }}>{children}</ul>
                  ),
                  ol: ({ children }) => (
                    <ol style={{ margin: "0 0 0.5rem", paddingLeft: "1.25em" }}>{children}</ol>
                  ),
                  a: ({ href, children }) => (
                    <a href={href} target="_blank" rel="noopener noreferrer">
                      {children}
                    </a>
                  ),
                }}
              >
                {folder.narrative_md ?? ""}
              </ReactMarkdown>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
