"use client";

// Small pill shown in the patient header when one or more proposals
// (consultation reviews) are awaiting the owner's decision. Clickable:
// navigates to the consultations list where the reviewer can drill in
// and approve / reject. Hidden when there is nothing pending or when
// the viewer is not the patient owner.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError, proposalsApi } from "@/lib/api";

interface Props {
  patientId: string;
  isOwner: boolean;
}

export default function PendingConsultsBadge({ patientId, isOwner }: Props) {
  const tH = useTranslations("historyPage");
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    if (!isOwner) {
      setCount(null);
      return;
    }
    let cancelled = false;
    proposalsApi
      .list(patientId, "open")
      .then((proposals) => {
        if (!cancelled) setCount(proposals.length);
      })
      .catch((e) => {
        // Best-effort: a 4xx/5xx here just means we don't show the
        // badge, not a hard error for the page.
        if (!cancelled && !(e instanceof ApiError)) {
          setCount(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, isOwner]);

  if (!isOwner || !count || count <= 0) return null;

  return (
    <Link
      href={`/patients/${patientId}/consultations`}
      title={tH("pendingConsultsTitle", { n: count })}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.35rem",
        padding: "0.25rem 0.6rem",
        borderRadius: 999,
        background: "#fef3c7",
        color: "#92400e",
        border: "1px solid #fcd34d",
        fontSize: "0.78rem",
        fontWeight: 600,
        textDecoration: "none",
      }}
    >
      <span>{tH("pendingConsultsLabel", { n: count })}</span>
    </Link>
  );
}
