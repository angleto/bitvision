"use client";

import { useTranslations } from "next-intl";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { ReactNode } from "react";
import { useCallback, useEffect, useState } from "react";

import CareTimeline from "@/components/CareTimeline";
import DocumentsV3Panel from "@/components/DocumentsV3Panel";
import EventCalendar from "@/components/EventCalendar";
import EvidenceWorkspace from "@/components/EvidenceWorkspace";
import MergedTimelineView from "@/components/MergedTimelineView";
import PatientAskPanel from "@/components/PatientAskPanel";
import ProvenanceTimeline from "@/components/ProvenanceTimeline";
import ShareLinksTable from "@/components/ShareLinksTable";
import TaskTimeline from "@/components/TaskTimeline";
import type { Patient } from "@/lib/api";
import {
  DEFAULT_VIEW,
  FASCICOLO_VIEWS,
  type View,
  parseView,
  viewKeys,
} from "@/lib/fascicoloViews";

interface Props {
  patient: Patient;
  isOwner?: boolean;
  driveSlot: ReactNode;
  initial?: View;
}

export default function FascicoloViewToggle({
  patient,
  isOwner = false,
  driveSlot,
  initial = DEFAULT_VIEW,
}: Props) {
  const [view, setView] = useState<View>(initial);
  const t = useTranslations("fascicolo.v3");
  // ``?view=`` survives refresh and browser-back from any sub-page so
  // the user lands back on the same tab they left. ``router.replace``
  // with ``scroll: false`` keeps the page from jumping when we swap
  // tabs (the body's own ``scrollIntoView`` runs on mount).
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Sync local state with the URL so browser back/forward restores
  // the right tab. Without this, the user clicks Eventi → Documenti,
  // hits browser back, the URL flips to ``?view=events`` but the
  // React state still shows Documenti as the selected tab and the
  // panel below renders the wrong content.
  useEffect(() => {
    setView(parseView(searchParams.get("view")));
  }, [searchParams]);

  const setViewPersisted = useCallback(
    (next: View) => {
      setView(next);
      const params = new URLSearchParams(searchParams);
      if (next === DEFAULT_VIEW) params.delete("view");
      else params.set("view", next);
      const qs = params.toString();
      // ``router.push`` (not ``replace``) so each tab change adds an
      // entry to the browser history. The user expects browser back
      // to undo the tab change — without this, back jumps past every
      // tab they've sampled to the previous page entirely. ``scroll:
      // false`` keeps the scroll position so switching tabs doesn't
      // throw the user back to the top.
      router.push(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname, searchParams],
  );

  // Order and i18n keys both come from ``FASCICOLO_VIEWS``; see that
  // module for why each tab sits where it does.
  const VIEWS = FASCICOLO_VIEWS.map((value) => {
    const keys = viewKeys(value);
    return { value, label: t(keys.tab), hint: t(keys.hint) };
  });

  // Merged-view toggle: when ``?merge=1`` is present alongside
  // ``?view=events`` or ``?view=tasks``, render the MergedTimelineView
  // instead of the dedicated CareTimeline / TaskTimeline. Both
  // entry-points hand the user the same merged surface, so the toggle
  // stays "in place" (no jarring tab switch when activating).
  const mergedActive = searchParams.get("merge") === "1";

  return (
    <div className="fascicolo-view-toggle">
      <div
        role="tablist"
        aria-label={t("treeRootLabel")}
        style={{
          display: "flex",
          gap: "0.25rem",
          marginBottom: "1rem",
          borderBottom: "1px solid var(--bv-card-border)",
          flexWrap: "wrap",
        }}
      >
        {VIEWS.map((v) => {
          const active = view === v.value;
          return (
            <button
              key={v.value}
              type="button"
              role="tab"
              aria-selected={active}
              aria-controls={`fascicolo-tabpanel-${v.value}`}
              id={`fascicolo-tab-${v.value}`}
              title={v.hint}
              onClick={() => setViewPersisted(v.value)}
              className={active ? undefined : "ghost"}
              style={{
                padding: "0.4rem 0.9rem",
                borderTopLeftRadius: "0.25rem",
                borderTopRightRadius: "0.25rem",
                borderBottomLeftRadius: 0,
                borderBottomRightRadius: 0,
                fontWeight: active ? 600 : 400,
                marginBottom: "-1px",
              }}
            >
              {v.label}
            </button>
          );
        })}
      </div>

      {view === "drive" && (
        <div role="tabpanel" id="fascicolo-tabpanel-drive" aria-labelledby="fascicolo-tab-drive">
          {driveSlot}
        </div>
      )}

      {view === "events" && (
        <section
          role="tabpanel"
          id="fascicolo-tabpanel-events"
          aria-labelledby="fascicolo-tab-events"
        >
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionEvents")}</p>
          {mergedActive ? (
            <MergedTimelineView patientId={patient.id} isOwner={isOwner} />
          ) : (
            <CareTimeline patientId={patient.id} isOwner={isOwner} />
          )}
        </section>
      )}

      {view === "tasks" && (
        <section
          role="tabpanel"
          id="fascicolo-tabpanel-tasks"
          aria-labelledby="fascicolo-tab-tasks"
        >
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionTasks")}</p>
          {mergedActive ? (
            <MergedTimelineView patientId={patient.id} isOwner={isOwner} />
          ) : (
            <TaskTimeline patientId={patient.id} isOwner={isOwner} />
          )}
        </section>
      )}

      {view === "calendar" && (
        <section
          role="tabpanel"
          id="fascicolo-tabpanel-calendar"
          aria-labelledby="fascicolo-tab-calendar"
        >
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionCalendar")}</p>
          <EventCalendar patientId={patient.id} isOwner={isOwner} />
        </section>
      )}

      {view === "documents" && (
        <section
          role="tabpanel"
          id="fascicolo-tabpanel-documents"
          aria-labelledby="fascicolo-tab-documents"
        >
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionDocuments")}</p>
          <DocumentsV3Panel patientId={patient.id} isOwner={isOwner} />
        </section>
      )}

      {view === "evidence" && (
        <section
          role="tabpanel"
          id="fascicolo-tabpanel-evidence"
          aria-labelledby="fascicolo-tab-evidence"
        >
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionEvidence")}</p>
          <EvidenceWorkspace patient={patient} />
        </section>
      )}

      {view === "provenance" && (
        <section
          role="tabpanel"
          id="fascicolo-tabpanel-provenance"
          aria-labelledby="fascicolo-tab-provenance"
        >
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionProvenance")}</p>
          <ProvenanceTimeline targetKind="patient" targetId={patient.id} />
        </section>
      )}

      {view === "ask" && (
        <section role="tabpanel" id="fascicolo-tabpanel-ask" aria-labelledby="fascicolo-tab-ask">
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionAsk")}</p>
          <PatientAskPanel patientId={patient.id} />
        </section>
      )}

      {view === "shares" && (
        <section
          role="tabpanel"
          id="fascicolo-tabpanel-shares"
          aria-labelledby="fascicolo-tab-shares"
        >
          <p style={{ color: "var(--bv-muted)", margin: "0 0 1rem" }}>{t("captionShares")}</p>
          <ShareLinksTable patientId={patient.id} />
        </section>
      )}
    </div>
  );
}
