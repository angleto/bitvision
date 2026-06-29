"use client";

// Render-side of the study detail. Decoupled from a specific Next route
// so both the legacy ``/studies/:id`` page (for backward-compat redirect
// + render fallback) and the canonical patient-namespaced
// ``/patients/:pid/studies/:sid`` page can share it.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import ComparePriorButton from "@/components/ComparePriorButton";
import ContextualBackLink from "@/components/ContextualBackLink";
import DeidentificationProvenancePanel from "@/components/DeidentificationProvenancePanel";
import LesionTracksPanel from "@/components/LesionTracksPanel";
import LicenseBadge from "@/components/LicenseBadge";
import NotesPanel from "@/components/NotesPanel";
import ReportUploadDialog from "@/components/ReportUploadDialog";
import ReportsList, { type ReportsListHandle } from "@/components/ReportsList";
import ResponseAssessmentCard from "@/components/ResponseAssessmentCard";
import SendStudyButton from "@/components/SendStudyButton";
import SeriesPreview from "@/components/SeriesPreview";
import ShareDialog from "@/components/ShareDialog";
import SimilarCasesPanel from "@/components/SimilarCasesPanel";
import StudyAttachedDocuments from "@/components/StudyAttachedDocuments";
import StudyExportButton from "@/components/StudyExportButton";
import StudyTagsSection from "@/components/StudyTagsSection";
import { ApiError, type StudyDetail, studiesApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

interface Props {
  studyId: string;
  /**
   * If the parent route already fetched the study (e.g. to enforce a
   * cross-patient guard), pass it down so this component skips its
   * own ``studiesApi.detail`` round-trip. When undefined, the
   * component fetches autonomously — this is what the legacy
   * ``/studies/:id`` redirect path relies on for its 404 fallback.
   */
  initialStudy?: StudyDetail;
}

export default function StudyDetailContent({ studyId, initialStudy }: Props) {
  const { user } = useAuth();
  const t = useTranslations("study");
  const tStudyDetail = useTranslations("studyDetail");
  const [study, setStudy] = useState<StudyDetail | null>(initialStudy ?? null);
  const [err, setErr] = useState<string | null>(null);
  const [reportDialogOpen, setReportDialogOpen] = useState(false);
  const [attachDialogOpen, setAttachDialogOpen] = useState(false);
  // Multi-select for the "open in compare" flow. Up to 4 series can
  // be loaded side-by-side in /viewer/compare; we wire a checkbox on
  // each series card plus a sticky bar that shows the selection
  // count and the launch button.
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<Set<string>>(() => new Set());
  const toggleSeriesSelected = (id: string) => {
    setSelectedSeriesIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const reportsListRef = useRef<ReportsListHandle | null>(null);

  useEffect(() => {
    if (initialStudy) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await studiesApi.detail(studyId);
        if (!cancelled) setStudy(data);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [studyId, initialStudy]);

  if (err)
    return (
      <main>
        <p className="error">{err}</p>
      </main>
    );
  if (!study)
    return (
      <main>
        <p className="meta">Loading…</p>
      </main>
    );

  return (
    <main>
      <p className="meta">
        {study.patient_id ? (
          <>
            <ContextualBackLink
              patientId={study.patient_id}
              patientName=""
              itemKind="study"
              itemId={studyId}
              rootLabel={t("patientFile")}
            />
            {" · "}
            <Link href="/studies" style={{ color: "var(--bv-muted)" }}>
              {t("allStudies")}
            </Link>
          </>
        ) : (
          <Link href="/studies">{t("backToStudies")}</Link>
        )}
      </p>
      <h1>
        {study.study_description ?? "(no description)"}
        <span className="badges">
          {study.modalities.map((m) => (
            <span key={m} className="badge">
              {m}
            </span>
          ))}
          {study.is_public && <span className="badge badge--public">public</span>}
          <span className="badge">tier {study.contribution_tier}</span>
          <LicenseBadge study={study} variant="header" />
        </span>
      </h1>
      <p className="meta">
        {study.study_date ?? "date unknown"} · {study.series.length} series · Study UID{" "}
        <code>{study.study_instance_uid}</code>
      </p>

      <p
        style={{
          marginBottom: "1.25rem",
          display: "flex",
          gap: "0.5rem",
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <StudyExportButton
          studyId={studyId}
          studyLabel={study.study_description}
          variant="button"
          stopPropagation={false}
        />
        {study.patient_id && (
          <SendStudyButton
            studyId={studyId}
            patientId={study.patient_id}
            studyLabel={study.study_description}
            variant="button"
            stopPropagation={false}
          />
        )}
      </p>

      {study.is_public && <DeidentificationProvenancePanel studyId={studyId} />}

      <StudyTagsSection
        studyId={studyId}
        canWrite={!!user && (user.is_admin || user.subject_id === study.owner_subject_id)}
      />

      <h2 style={{ marginTop: "2rem" }}>Series</h2>
      {/* Sticky action bar with two distinct open modes when 2+
          series are selected:
            - Fusion / overlay: 2 series where at least one is PT.
              * PT + CT: standard oncology read, CT primary (gray
                anatomic base) + PT colour overlay.
              * PT + PT: NAC vs CTAC reconstruction comparison or
                two reconstructions of the same study. Standard
                clinical case is QA on the CTAC AC artefacts at
                metal hardware (compare CTAC against NAC). The AC
                / CTAC reconstruction goes as primary, the NAC /
                other as overlay; description heuristics pick the
                right one, with the first selected as fallback when
                neither label is identifiable.
              Routes to ``/viewer/series/<primary>?fusion=<overlay>``.
            - Multi-series: side-by-side panes, each running the
              full Cornerstone viewer with its own tool group.
              Routes to ``/viewer/multi?s=<id>&s=<id>...``. */}
      {(() => {
        const selectedSeries = study.series.filter((s) => selectedSeriesIds.has(s.id));
        const ptSeries = selectedSeries.filter((s) => (s.modality ?? "").toUpperCase() === "PT");
        const ctSeries = selectedSeries.filter((s) => (s.modality ?? "").toUpperCase() === "CT");
        // Description hints used when 2 PT series are paired:
        //   AC / CTAC / ATTEN(uation corrected) → primary
        //   NAC / NON-ATTEN(uation) → overlay
        // ``isAc`` excludes anything that is explicitly NAC.
        const ptDescIsAc = (desc: string | null | undefined) => {
          const d = (desc ?? "").toUpperCase();
          if (/NAC|NON.?ATTEN/.test(d)) return false;
          return /CTAC|\bAC\b|ATTN|ATTENU/.test(d);
        };
        const ptDescIsNac = (desc: string | null | undefined) => {
          const d = (desc ?? "").toUpperCase();
          return /NAC|NON.?ATTEN/.test(d);
        };

        let fusionEligible = false;
        let fusionPrimaryId: string | null = null;
        let fusionOverlayId: string | null = null;

        if (selectedSeriesIds.size === 2) {
          if (ptSeries.length === 1 && ctSeries.length === 1) {
            fusionEligible = true;
            fusionPrimaryId = ctSeries[0].id;
            fusionOverlayId = ptSeries[0].id;
          } else if (ptSeries.length === 2) {
            fusionEligible = true;
            const [a, b] = ptSeries;
            const aAc = ptDescIsAc(a.series_description);
            const bAc = ptDescIsAc(b.series_description);
            const aNac = ptDescIsNac(a.series_description);
            const bNac = ptDescIsNac(b.series_description);
            if (aAc && bNac) {
              fusionPrimaryId = a.id;
              fusionOverlayId = b.id;
            } else if (bAc && aNac) {
              fusionPrimaryId = b.id;
              fusionOverlayId = a.id;
            } else if (aAc && !bAc) {
              fusionPrimaryId = a.id;
              fusionOverlayId = b.id;
            } else if (bAc && !aAc) {
              fusionPrimaryId = b.id;
              fusionOverlayId = a.id;
            } else if (aNac && !bNac) {
              // Only NAC is labelled; the other PT is presumably AC.
              fusionPrimaryId = b.id;
              fusionOverlayId = a.id;
            } else if (bNac && !aNac) {
              fusionPrimaryId = a.id;
              fusionOverlayId = b.id;
            } else {
              fusionPrimaryId = a.id;
              fusionOverlayId = b.id;
            }
          }
        }

        const fusionUrl =
          fusionEligible && fusionPrimaryId && fusionOverlayId
            ? `/viewer/series/${fusionPrimaryId}?fusion=${fusionOverlayId}`
            : "#";
        const multiUrl = `/viewer/multi?${Array.from(selectedSeriesIds)
          .slice(0, 4)
          .map((id) => `s=${id}`)
          .join("&")}`;
        const compareIds = Array.from(selectedSeriesIds).slice(0, 2);
        const compareUrl =
          compareIds.length === 2
            ? `/viewer/followup?baseline=${compareIds[0]}&followup=${compareIds[1]}`
            : "#";
        // The multiphase contrast viewer opens this study's CT phases. By
        // default it auto-classifies them, but if the radiologist ticked the
        // exact series to compare we honour that pick: pass the selected CT
        // series so the viewer opens precisely those (the auto-detection can
        // be wrong, so the manual choice must win).
        const ctCount = study.series.filter(
          (s) => (s.modality ?? "").toUpperCase() === "CT",
        ).length;
        const contrastSel = ctSeries.map((s) => s.id);
        const contrastUrl =
          contrastSel.length >= 2
            ? `/viewer/contrast?study=${studyId}&${contrastSel.map((id) => `s=${id}`).join("&")}`
            : `/viewer/contrast?study=${studyId}`;
        return (
          <div
            style={{
              position: "sticky",
              top: 0,
              zIndex: 5,
              display: "flex",
              alignItems: "center",
              gap: "0.6rem",
              padding: "0.5rem 0.75rem",
              background: "var(--bv-card-bg, #fff)",
              border: "1px solid var(--bv-card-border, #e5e7eb)",
              borderRadius: 6,
              marginBottom: "0.5rem",
              flexWrap: "wrap",
            }}
          >
            <strong style={{ fontSize: "0.85rem" }}>
              {selectedSeriesIds.size === 0
                ? "Select series to open"
                : selectedSeriesIds.size === 1
                  ? "1 series selected — pick one more for fusion"
                  : `${selectedSeriesIds.size} series selected`}
            </strong>
            {selectedSeriesIds.size > 0 && (
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.78rem" }}
                onClick={() => setSelectedSeriesIds(new Set())}
              >
                Cancel
              </button>
            )}
            <button
              type="button"
              className="ghost"
              style={{ fontSize: "0.78rem" }}
              disabled={selectedSeriesIds.size === study.series.length && study.series.length > 0}
              onClick={() => setSelectedSeriesIds(new Set(study.series.map((s) => s.id)))}
              title={
                selectedSeriesIds.size === study.series.length
                  ? "All series already selected"
                  : `Select all ${study.series.length} series`
              }
            >
              Select all
            </button>
            <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
              <Link
                href={fusionUrl}
                style={{
                  fontSize: "0.82rem",
                  padding: "0.3rem 0.8rem",
                  borderRadius: 6,
                  border: "1px solid var(--bv-accent, #e96b1f)",
                  color: fusionEligible ? "#fff" : "var(--bv-muted, #888)",
                  background: fusionEligible ? "var(--bv-accent, #e96b1f)" : "transparent",
                  textDecoration: "none",
                  opacity: fusionEligible ? 1 : 0.45,
                  pointerEvents: fusionEligible ? "auto" : "none",
                }}
                title={
                  fusionEligible
                    ? ptSeries.length === 2
                      ? "Open both PT reconstructions as overlay (CTAC primary if identifiable, NAC overlay)"
                      : "Open the CT as anatomic base with the PT as colour overlay (PET-CT fusion)"
                    : "Pick 2 series: PT+CT for fusion, or 2 PT for reconstruction overlay"
                }
              >
                {fusionEligible && ptSeries.length === 2
                  ? "Open as PT overlay →"
                  : "Open as PET-CT fusion →"}
              </Link>
              <Link
                href={multiUrl}
                style={{
                  fontSize: "0.82rem",
                  padding: "0.3rem 0.8rem",
                  borderRadius: 6,
                  border: "1px solid var(--bv-card-border, #d0d5dd)",
                  color: "inherit",
                  background: "transparent",
                  textDecoration: "none",
                  opacity: selectedSeriesIds.size >= 2 ? 1 : 0.45,
                  pointerEvents: selectedSeriesIds.size >= 2 ? "auto" : "none",
                }}
                title="Open the selected series side-by-side, each with its own full tool set"
              >
                Open as multi-series →
              </Link>
              <Link
                href={compareUrl}
                style={{
                  fontSize: "0.82rem",
                  padding: "0.3rem 0.8rem",
                  borderRadius: 6,
                  border: "1px solid var(--bv-card-border, #d0d5dd)",
                  color: "inherit",
                  background: "transparent",
                  textDecoration: "none",
                  opacity: compareIds.length === 2 ? 1 : 0.45,
                  pointerEvents: compareIds.length === 2 ? "auto" : "none",
                }}
                title="Compare two series side-by-side with a synchronised crosshair (baseline vs follow-up)"
              >
                Compare follow-up →
              </Link>
              <Link
                href={contrastUrl}
                style={{
                  fontSize: "0.82rem",
                  padding: "0.3rem 0.8rem",
                  borderRadius: 6,
                  border: "1px solid var(--bv-card-border, #d0d5dd)",
                  color: "inherit",
                  background: "transparent",
                  textDecoration: "none",
                  opacity: ctCount >= 2 ? 1 : 0.45,
                  pointerEvents: ctCount >= 2 ? "auto" : "none",
                }}
                title={
                  contrastSel.length >= 2
                    ? `Open the ${contrastSel.length} selected CT series as contrast phases, synced by anatomy and auto-windowed`
                    : "Open this CT study's contrast phases side-by-side, synced by anatomy and auto-windowed (non-contrast / arterial / portal / delayed). Tick 2+ CT series to open exactly those."
                }
              >
                {contrastSel.length >= 2
                  ? `Contrast phases (${contrastSel.length}) →`
                  : "Contrast phases →"}
              </Link>
              {study.patient_id && (
                <ComparePriorButton
                  patientId={study.patient_id}
                  currentStudyId={studyId}
                  currentSeriesId={
                    study.series.find((s) => (s.modality ?? "").toUpperCase() === "CT")?.id
                  }
                />
              )}
            </div>
          </div>
        );
      })()}
      {study.patient_id && (
        <div
          style={{
            display: "grid",
            gap: "var(--bv-s-3, 0.75rem)",
            marginBottom: "var(--bv-s-3, 0.75rem)",
          }}
        >
          <ResponseAssessmentCard patientId={study.patient_id} currentStudyId={studyId} />
          <LesionTracksPanel
            patientId={study.patient_id}
            followupSeriesId={
              study.series.find((s) => (s.modality ?? "").toUpperCase() === "CT")?.id
            }
          />
        </div>
      )}
      <div className="series-grid">
        {study.series.map((s) => {
          const checked = selectedSeriesIds.has(s.id);
          // Card layout mirrors the patient-view fascicolo cards
          // (``ContentPane.tsx``): a top header strip carries the
          // selection checkbox, the preview thumbnail sits below
          // unobstructed. Previously the checkbox was absolute-
          // positioned over the preview and clashed with the modality
          // badge SeriesPreview draws in its top-left corner.
          return (
            <div
              key={s.id}
              className="card series-card"
              style={{
                color: "inherit",
                outline: checked ? "2px solid var(--bv-accent, #e96b1f)" : undefined,
                outlineOffset: -2,
                padding: 0,
                margin: 0,
                overflow: "hidden",
                display: "flex",
                flexDirection: "column",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 4,
                  padding: "4px 6px",
                  minHeight: 28,
                  borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
                  background: "var(--bv-card-bg, #fff)",
                }}
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => e.stopPropagation()}
              >
                <label
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    cursor: "pointer",
                    padding: "0 2px",
                    fontSize: "0.7rem",
                  }}
                  title={tStudyDetail("selectForCompareTitle")}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleSeriesSelected(s.id)}
                  />
                </label>
              </div>
              <div style={{ padding: "0 0.75rem" }}>
                <SeriesPreview seriesId={s.id} sliceCount={s.received_instance_count || 1} />
              </div>
              <div style={{ padding: "0.5rem 0.75rem 0.75rem" }}>
                <h3 style={{ fontSize: "0.9rem" }}>
                  <Link
                    href={`/viewer/series/${s.id}`}
                    style={{ color: "inherit", textDecoration: "none" }}
                  >
                    #{s.series_number ?? "?"} — {s.series_description ?? "(no description)"}
                  </Link>
                </h3>
                <div className="meta" style={{ fontSize: "0.8rem" }}>
                  <span className="badges" style={{ marginLeft: 0 }}>
                    {s.modality && <span className="badge">{s.modality}</span>}
                    {s.body_part_examined && <span className="badge">{s.body_part_examined}</span>}
                  </span>{" "}
                  {s.received_instance_count} slice
                  {s.received_instance_count === 1 ? "" : "s"}
                </div>
                <div style={{ marginTop: "0.5rem" }}>
                  <Link
                    href={`/viewer/series/${s.id}`}
                    style={{
                      display: "inline-block",
                      fontSize: "0.78rem",
                      padding: "0.25rem 0.7rem",
                      borderRadius: 6,
                      border: "1px solid var(--bv-card-border)",
                      color: "inherit",
                      textDecoration: "none",
                    }}
                  >
                    {tStudyDetail("openInViewer")}
                  </Link>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <h2
        style={{
          marginTop: "2rem",
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          flexWrap: "wrap",
        }}
      >
        <span>Reports</span>
        {user && (
          <>
            <button
              type="button"
              className="ghost"
              style={{ fontSize: "0.85rem" }}
              onClick={() => setReportDialogOpen(true)}
              title={tStudyDetail("uploadNewReportTitle")}
            >
              {tStudyDetail("uploadNewReport")}
            </button>
            {study.patient_id && (
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.85rem" }}
                onClick={() => setAttachDialogOpen(true)}
                title={tStudyDetail("attachExistingReportTitle")}
              >
                {tStudyDetail("attachExistingReport")}
              </button>
            )}
          </>
        )}
      </h2>
      <ReportsList ref={reportsListRef} studyId={studyId} />
      <ReportUploadDialog
        studyId={studyId}
        open={reportDialogOpen}
        onClose={() => setReportDialogOpen(false)}
        onCreated={() => {
          reportsListRef.current?.refresh();
        }}
      />

      {study.patient_id && (
        <StudyAttachedDocuments
          patientId={study.patient_id}
          studyId={studyId}
          canWrite={!!user && (user.is_admin || user.subject_id === study.owner_subject_id)}
          externalDialogOpen={attachDialogOpen}
          onExternalDialogClose={() => setAttachDialogOpen(false)}
        />
      )}

      <h2 style={{ marginTop: "2rem" }}>Similar cases</h2>
      <SimilarCasesPanel targetId={studyId} />

      {study.patient_id && (
        <NotesPanel patientId={study.patient_id} targetKind="study" targetId={studyId} />
      )}

      <ShareDialog
        studyId={studyId}
        isOwner={!!user && (user.is_admin || user.subject_id === study.owner_subject_id)}
      />
    </main>
  );
}
