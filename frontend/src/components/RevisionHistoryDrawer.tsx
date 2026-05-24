"use client";

// Slide-out drawer for the patient revision history. Accessible via a
// button on the patient page; not always visible. Lets the clinician
// scroll through the commits like ``git log`` and "go to" a specific
// version, surfacing the entities at that commit inline. Detail-panel
// actions (Annulla questa revisione, Ripristina per entità) live in
// ``CommitDetailPanel`` so the full history page can reuse them.
//
// Default mode is clinical-language. A toggle exposes the underlying
// commit hashes / branches for advanced users (``versioning:advanced``
// permission, future).

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import CommitDetailPanel from "@/components/CommitDetailPanel";
import HistoryTimeline from "@/components/HistoryTimeline";
import type { CommitOut } from "@/lib/api";

interface Props {
  patientId: string;
  open: boolean;
  onClose: () => void;
}

export default function RevisionHistoryDrawer({ patientId, open, onClose }: Props) {
  const tH = useTranslations("historyPage");
  const [advanced, setAdvanced] = useState(false);
  const [selected, setSelected] = useState<CommitOut | null>(null);
  // Bumped on every successful mutation so HistoryTimeline remounts and
  // refetches. Cheap and avoids threading a refresh callback through.
  const [refreshKey, setRefreshKey] = useState(0);

  // Reset selection when drawer is closed so re-opening starts clean.
  useEffect(() => {
    if (!open) {
      setSelected(null);
    }
  }, [open]);

  // Close on Escape for keyboard accessibility.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const onMutated = () => {
    // Pop back to the timeline view and force a refetch.
    setSelected(null);
    setRefreshKey((k) => k + 1);
  };

  return (
    <>
      {/* Backdrop: aria-hidden because the keyboard escape is via
          the drawer's own elements (focus + Esc closes). The
          suppression below documents that the click handler is the
          mouse-only "click-outside-to-dismiss" affordance, mirrored
          by Esc keyboard handling on the drawer itself. */}
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: backdrop click-to-dismiss; keyboard equivalent is Esc on the focused drawer content. */}
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.35)",
          zIndex: 99,
        }}
        aria-hidden="true"
      />
      <aside
        // biome-ignore lint/a11y/useSemanticElements: side-drawer, not a centered modal; native <dialog> centers and would override the drawer's right-anchored layout. Escape close + backdrop click handled above.
        role="dialog"
        aria-label={tH("drawerLabel")}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: "min(440px, 100vw)",
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, inherit)",
          boxShadow: "-4px 0 18px rgba(0,0,0,0.12)",
          zIndex: 100,
          display: "flex",
          flexDirection: "column",
        }}
      >
        <header
          style={{
            padding: "0.85rem 1rem",
            borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "0.6rem",
          }}
        >
          <div>
            <h2 style={{ margin: 0, fontSize: "1rem" }}>{tH("drawerTitle")}</h2>
            <p className="meta" style={{ margin: "0.15rem 0 0", fontSize: "0.72rem" }}>
              {tH("drawerIntro")}
            </p>
          </div>
          <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
            <label
              style={{
                fontSize: "0.7rem",
                display: "inline-flex",
                gap: "0.2rem",
                alignItems: "center",
                cursor: "pointer",
              }}
              title={tH("drawerAdvancedTitle")}
            >
              <input
                type="checkbox"
                checked={advanced}
                onChange={(e) => setAdvanced(e.target.checked)}
              />
              {tH("drawerAdvanced")}
            </label>
            <button
              type="button"
              onClick={onClose}
              aria-label={tH("drawerClose")}
              style={{
                background: "transparent",
                border: "none",
                fontSize: "1.2rem",
                cursor: "pointer",
                color: "inherit",
                padding: "0.2rem 0.4rem",
              }}
            >
              ×
            </button>
          </div>
        </header>

        <div
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "0.7rem 0.85rem",
          }}
        >
          {!selected && (
            <HistoryTimeline
              key={refreshKey}
              patientId={patientId}
              advanced={advanced}
              onSelect={setSelected}
              branchAware
            />
          )}
          {selected && (
            <CommitDetailPanel
              patientId={patientId}
              commit={selected}
              advanced={advanced}
              onBack={() => setSelected(null)}
              onMutated={onMutated}
            />
          )}
        </div>

        <footer
          style={{
            padding: "0.7rem 1rem",
            borderTop: "1px solid var(--bv-card-border, #e5e7eb)",
            fontSize: "0.78rem",
          }}
        >
          <Link
            href={`/patients/${patientId}/history${advanced ? "?advanced=1" : ""}`}
            onClick={onClose}
          >
            {tH("drawerOpenFull")}
          </Link>
        </footer>
      </aside>
    </>
  );
}
