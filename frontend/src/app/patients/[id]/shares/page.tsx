"use client";

// /patients/{id}/shares — share-link list narrowed to a single
// patient. Same component as /settings/shares with the patient_id
// filter applied, so the operator can quickly audit "which links
// are out there for this fascicolo" without scrolling the global
// table.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";

import ShareLinksTable from "@/components/ShareLinksTable";

export default function PatientSharesPage() {
  const t = useTranslations("settingsShares");
  const params = useParams<{ id: string }>();
  const patientId = params?.id;
  if (!patientId) return null;
  return (
    <main>
      <p className="meta">
        <Link href={`/patients/${patientId}`}>← {t("backToPatient")}</Link>
      </p>
      <h1>{t("titlePatient")}</h1>
      <p style={{ marginBottom: "1rem", opacity: 0.8 }}>{t("subtitlePatient")}</p>
      <ShareLinksTable patientId={patientId} />
    </main>
  );
}
