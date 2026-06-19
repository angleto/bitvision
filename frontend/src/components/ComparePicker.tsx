"use client";

// Pre-load series picker for the follow-up comparison viewer.
//
// The comparison viewer must NOT silently fetch two multi-GB volumes for
// series it guessed: on the throttled production egress that is minutes of
// wasted transfer if the guess is wrong. So unless a confident medical default
// exists (handled upstream by ``matchConfidence``), the viewer shows this
// panel first and loads nothing until the user confirms. Each side lists the
// series of its study with the medical label (modality · plane · phase phrasing
// · #img), the best medical guess pre-selected and badged, and the comparison
// only starts on "Load". Pure presentational; the parent owns selection state.

import type { Series } from "@/lib/api";
import { type MatchReason, type PrimaryPlane, seriesOptionLabel } from "@/lib/seriesMatch";
import { useTranslations } from "next-intl";

interface SidePane {
  list: Series[];
  planeOf: (id: string) => PrimaryPlane | null;
  study: { study_date: string | null; study_description: string | null } | null;
}

export interface ComparePickerProps {
  baseline: SidePane;
  followup: SidePane;
  baselineId: string | null;
  followupId: string | null;
  /** Series the medical match suggests on each side (badged, not auto-loaded). */
  suggestedBaselineId: string | null;
  suggestedFollowupId: string | null;
  /** Why the picker is shown, so it can explain itself (null = generic). */
  reason: MatchReason | null;
  onPickBaseline: (id: string) => void;
  onPickFollowup: (id: string) => void;
  onConfirm: () => void;
}

export default function ComparePicker({
  baseline,
  followup,
  baselineId,
  followupId,
  suggestedBaselineId,
  suggestedFollowupId,
  reason,
  onPickBaseline,
  onPickFollowup,
  onConfirm,
}: ComparePickerProps) {
  const t = useTranslations("compare");
  const planeLabel = (p: PrimaryPlane | null): string | null =>
    p && p !== "unknown" ? t(`plane.${p}`) : null;

  // Contextual sub-line: ambiguity vs no medical match vs generic.
  const hint =
    reason === "ambiguous"
      ? t("pick.hintAmbiguous")
      : reason === "no-modality-match"
        ? t("pick.hintNoMatch")
        : t("pick.subtitle");

  const canLoad = !!baselineId && !!followupId;

  return (
    <div
      style={{
        flex: "1 1 auto",
        overflow: "auto",
        background: "#000",
        color: "var(--bv-fg, #e6ecf3)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: "1.5rem",
      }}
    >
      <div style={{ width: "100%", maxWidth: 980 }}>
        <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.25rem" }}>{t("pick.title")}</h2>
        <p className="meta" style={{ margin: "0 0 1.25rem", color: "#94a3b8" }}>
          {hint}
        </p>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
          <PickerColumn
            sideLabel={t("baseline")}
            pane={baseline}
            selectedId={baselineId}
            suggestedId={suggestedBaselineId}
            onPick={onPickBaseline}
            planeLabel={planeLabel}
            suggestedBadge={t("pick.suggested")}
            emptyLabel={t("pick.empty")}
          />
          <PickerColumn
            sideLabel={t("followup")}
            pane={followup}
            selectedId={followupId}
            suggestedId={suggestedFollowupId}
            onPick={onPickFollowup}
            planeLabel={planeLabel}
            suggestedBadge={t("pick.suggested")}
            emptyLabel={t("pick.empty")}
          />
        </div>

        <div style={{ marginTop: "1.25rem", display: "flex", justifyContent: "flex-end" }}>
          <button
            type="button"
            onClick={onConfirm}
            disabled={!canLoad}
            style={{
              background: canLoad ? "var(--bv-accent, #2563eb)" : "#1f2937",
              color: canLoad ? "#fff" : "#6b7280",
              border: "none",
              borderRadius: 8,
              padding: "0.55rem 1.2rem",
              fontSize: "0.9rem",
              cursor: canLoad ? "pointer" : "not-allowed",
            }}
          >
            {t("pick.load")}
          </button>
        </div>
      </div>
    </div>
  );
}

function PickerColumn({
  sideLabel,
  pane,
  selectedId,
  suggestedId,
  onPick,
  planeLabel,
  suggestedBadge,
  emptyLabel,
}: {
  sideLabel: string;
  pane: SidePane;
  selectedId: string | null;
  suggestedId: string | null;
  onPick: (id: string) => void;
  planeLabel: (p: PrimaryPlane | null) => string | null;
  suggestedBadge: string;
  emptyLabel: string;
}) {
  const studyLine = [pane.study?.study_date, pane.study?.study_description]
    .filter(Boolean)
    .join(" · ");
  return (
    <div
      style={{
        border: "1px solid var(--bv-card-border, #1a1f2b)",
        borderRadius: 8,
        background: "var(--bv-card-bg, #11151c)",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div style={{ padding: "0.6rem 0.75rem", borderBottom: "1px solid #1a1f2b" }}>
        <div style={{ fontSize: "0.8rem", color: "#94a3b8" }}>{sideLabel}</div>
        {studyLine && (
          <div style={{ fontSize: "0.82rem", color: "#cbd5e1", marginTop: 2 }}>{studyLine}</div>
        )}
      </div>
      <div style={{ maxHeight: "46vh", overflow: "auto" }}>
        {pane.list.length === 0 ? (
          <div style={{ padding: "0.75rem", color: "#6b7280", fontSize: "0.82rem" }}>
            {emptyLabel}
          </div>
        ) : (
          pane.list.map((s) => {
            const active = s.id === selectedId;
            return (
              <button
                type="button"
                key={s.id}
                onClick={() => onPick(s.id)}
                aria-pressed={active}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                  width: "100%",
                  textAlign: "left",
                  padding: "0.5rem 0.75rem",
                  fontSize: "0.8rem",
                  cursor: "pointer",
                  border: "none",
                  borderBottom: "1px solid #141922",
                  background: active ? "var(--bv-accent-soft, #1e293b)" : "transparent",
                  color: active ? "#fff" : "#cbd5e1",
                }}
              >
                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {seriesOptionLabel(s, planeLabel(pane.planeOf(s.id)))}
                </span>
                {s.id === suggestedId && (
                  <span
                    className="badge"
                    style={{
                      flex: "0 0 auto",
                      color: "var(--bv-success, #34d399)",
                      borderColor: "var(--bv-success, #34d399)",
                      fontSize: "0.7rem",
                    }}
                  >
                    {suggestedBadge}
                  </span>
                )}
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
