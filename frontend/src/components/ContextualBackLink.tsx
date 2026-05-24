"use client";

// Single source of truth for the "← back" affordance at the top of
// every detail page reachable from inside the patient fascicolo
// (study, document, clinical-event, ...).
//
// Reads ``?from=`` (and the companion params) from the URL and emits
// the most informative back-link the page came through, falling back
// to the folder / fascicolo chain via ``BackToFolderLink`` when the
// detail page is opened cold (deep-link, refresh).
//
// Supported origins:
//
//   ?from=care-phase&phase=<slug>&phaseName=<encoded>
//     → "← Fase: <name>" → /patients/{id}/care-phases/{slug}
//
//   ?from=timeline
//     → "← Timeline eventi" → /patients/{id}?view=events
//
//   ?from=documents
//     → "← Documenti" → /patients/{id}?view=documents
//
//   ?from=event&event=<id>
//     → "← Torna all'evento" → /patients/{patientId}/clinical-events/{event_id}
//
//   ?from=notes&note=<id>
//     → "← Torna alle evidenze" → /patients/{id}?view=evidence#note-{note}
//
//   (no ``from``) → BackToFolderLink (parent folder · fascicolo).
//
// Whatever the primary back-link, the parent folder / fascicolo is
// always rendered as a secondary chip so the user has a second
// navigation path without having to use the browser-back history.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import BackToFolderLink from "@/components/BackToFolderLink";

type ItemKind = "folder" | "study" | "series" | "document" | "report" | "consultation";

interface Props {
  patientId: string;
  patientName: string;
  itemKind: ItemKind;
  itemId: string;
  /** Override the root label rendered when no folder context applies
   *  (e.g. study detail prefers ``Fascicolo paziente``). */
  rootLabel?: string;
}

export default function ContextualBackLink({
  patientId,
  patientName,
  itemKind,
  itemId,
  rootLabel,
}: Props) {
  const search = useSearchParams();
  const tFasc = useTranslations("fascicolo");
  const tCb = useTranslations("contextualBackLink");
  const from = search.get("from");

  if (from === "care-phase") {
    const slug = search.get("phase");
    const name = search.get("phaseName") ?? slug ?? "";
    if (slug) {
      return (
        <>
          <Link href={`/patients/${patientId}/care-phases/${slug}`}>
            &larr; {name ? tCb("phaseLabel", { name }) : tCb("phaseGeneric")}
          </Link>
          {" · "}
          <span style={{ color: "var(--bv-muted)" }}>
            <BackToFolderLink
              patientId={patientId}
              patientName={patientName}
              itemKind={itemKind}
              itemId={itemId}
              rootLabel={rootLabel}
            />
          </span>
        </>
      );
    }
  }

  if (from === "timeline") {
    return (
      <>
        <Link href={`/patients/${patientId}?view=events`}>{tCb("timelineEvents")}</Link>
        {" · "}
        <span style={{ color: "var(--bv-muted)" }}>
          <BackToFolderLink
            patientId={patientId}
            patientName={patientName}
            itemKind={itemKind}
            itemId={itemId}
            rootLabel={rootLabel}
          />
        </span>
      </>
    );
  }

  if (from === "documents") {
    return (
      <>
        <Link href={`/patients/${patientId}?view=documents`}>{tCb("documents")}</Link>
        {" · "}
        <span style={{ color: "var(--bv-muted)" }}>
          <BackToFolderLink
            patientId={patientId}
            patientName={patientName}
            itemKind={itemKind}
            itemId={itemId}
            rootLabel={rootLabel}
          />
        </span>
      </>
    );
  }

  if (from === "event") {
    const eventId = search.get("event");
    if (eventId) {
      return (
        <>
          <Link href={`/patients/${patientId}/clinical-events/${eventId}`}>
            {tCb("backToEvent")}
          </Link>
          {" · "}
          <span style={{ color: "var(--bv-muted)" }}>
            <BackToFolderLink
              patientId={patientId}
              patientName={patientName}
              itemKind={itemKind}
              itemId={itemId}
              rootLabel={rootLabel}
            />
          </span>
        </>
      );
    }
  }

  if (from === "notes") {
    const noteId = search.get("note");
    return (
      <>
        <Link href={`/patients/${patientId}?view=evidence${noteId ? `#note-${noteId}` : ""}`}>
          {tCb("backToEvidence")}
        </Link>
        {" · "}
        <Link href={`/patients/${patientId}`} style={{ color: "var(--bv-muted)" }}>
          {tFasc("treeRootLabel").toLowerCase()}
        </Link>
      </>
    );
  }

  return (
    <BackToFolderLink
      patientId={patientId}
      patientName={patientName}
      itemKind={itemKind}
      itemId={itemId}
      rootLabel={rootLabel}
    />
  );
}
