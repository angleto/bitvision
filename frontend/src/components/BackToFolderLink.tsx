"use client";

// Contextual back-link rendered at the top of detail pages
// (document / study / consultation / report …) reachable from inside
// a fascicolo folder. When the leaf is filed inside a folder, the link
// chain reads "← <folder> · <fascicolo>" and clicking the folder link
// returns to the same path the user was browsing in the fascicolo via
// the ``?path=`` query that ``FascicoloDriveLayout`` consumes. When
// the leaf lives at the patient root, the chain collapses to a single
// "← Fascicolo di <patient>" link so there's no noise.
//
// The component owns its own breadcrumb fetch (idempotent on the
// patient/item pair); failures fall back to the patient-root link
// without surfacing an error so the back-link is never broken.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { type BreadcrumbSegment, patientTreeApi } from "@/lib/api";

type ItemKind = "folder" | "study" | "series" | "document" | "report" | "consultation";

interface Props {
  patientId: string;
  patientName: string;
  itemKind: ItemKind;
  itemId: string;
  /**
   * Optional alternative anchor text rendered when the parent is the
   * patient root (no folder). Defaults to ``Fascicolo di {name}`` /
   * ``{name}'s health record`` via the ``fascicolo.documentDetail``
   * namespace; pass a custom string when a page wants a different
   * default (e.g. study detail's ``← Fascicolo paziente``).
   */
  rootLabel?: string;
}

export default function BackToFolderLink({
  patientId,
  patientName,
  itemKind,
  itemId,
  rootLabel,
}: Props) {
  const tFasc = useTranslations("fascicolo");
  const [breadcrumb, setBreadcrumb] = useState<BreadcrumbSegment[] | null>(null);

  useEffect(() => {
    if (!itemId) return;
    let cancelled = false;
    patientTreeApi
      .breadcrumbForItem(patientId, itemKind, itemId)
      .then((segments) => {
        if (!cancelled) setBreadcrumb(segments);
      })
      .catch(() => {
        if (!cancelled) setBreadcrumb([]);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, itemKind, itemId]);

  // The synthetic root has ``id == null`` and ``path == "/"``; a real
  // parent folder has both fields set. Anything else (legacy null on a
  // non-root segment, missing breadcrumb) is treated as "no folder".
  const parentFolder = useMemo(() => {
    if (!breadcrumb || breadcrumb.length === 0) return null;
    const last = breadcrumb[breadcrumb.length - 1];
    if (!last || last.path === "/" || !last.id) return null;
    return last;
  }, [breadcrumb]);

  const fascicoloLabel = rootLabel ?? tFasc("documentDetail.fascicoloOf", { name: patientName });

  // ``#item-<itemId>`` rides along on the back-link so the fascicolo
  // can highlight (and scroll into view) the card the user just came
  // back from. Browser back-button navigation hits the same code path
  // because Next preserves the hash on history pop. ``itemId`` is the
  // resource uuid (study / document / consultation), matching the
  // ``data-item-id`` attribute the cards render.
  const itemHash = itemId ? `#item-${itemId}` : "";
  if (!parentFolder) {
    return <Link href={`/patients/${patientId}${itemHash}`}>&larr; {fascicoloLabel}</Link>;
  }
  const folderUrl = `/patients/${patientId}?path=${encodeURIComponent(parentFolder.path)}${itemHash}`;
  return (
    <>
      <Link href={folderUrl}>&larr; {parentFolder.name}</Link>
      {" · "}
      <Link href={`/patients/${patientId}`} style={{ color: "var(--bv-muted)" }}>
        {fascicoloLabel}
      </Link>
    </>
  );
}
