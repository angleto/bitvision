"use client";

// Persistent "Back to Evidenze e sintesi" chip rendered at the top of
// any destination page reachable from a clinical-note mention. The
// chip reads ``?ctx=evidence:...`` off the URL; the rest of the page
// stays untouched, so consumers only mount the chip once and forget
// about it.
//
// Three variants of the ``ctx`` token are recognised:
//
//   evidence                   plain (no source note id, generic back)
//   evidence:{patientId}       same as above (no anchor)
//   evidence:note:{noteId}     anchor the destination at #note-{id}
//                              so the user lands on the exact source row
//   evidence:tag:{value}       came from a tag aggregation page; jump
//                              back to that tag instead of the index
//
// Anything that doesn't start with "evidence" is ignored — the chip
// simply renders nothing, matching the "no ctx" path.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

interface Props {
  patientId: string;
}

export default function BackToEvidenceChip({ patientId }: Props) {
  const t = useTranslations("evidence");
  const search = useSearchParams();
  const ctx = search.get("ctx");
  if (!ctx || !ctx.startsWith("evidence")) return null;

  const href = (() => {
    if (ctx.startsWith("evidence:note:")) {
      const id = ctx.slice("evidence:note:".length);
      return `/patients/${patientId}#note-${encodeURIComponent(id)}`;
    }
    if (ctx.startsWith("evidence:tag:")) {
      const value = ctx.slice("evidence:tag:".length);
      return `/patients/${patientId}/tags/${encodeURIComponent(value)}`;
    }
    return `/patients/${patientId}`;
  })();

  return (
    <Link
      href={href}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.4rem",
        background: "var(--bv-info-soft, #eef4ff)",
        color: "var(--bv-info, #1e40af)",
        borderRadius: 999,
        padding: "3px 12px",
        fontSize: "0.82rem",
        fontWeight: 500,
        textDecoration: "none",
        marginBottom: "0.6rem",
      }}
    >
      ← {t("backToEvidence")}
    </Link>
  );
}
