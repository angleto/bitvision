"use client";

// /settings/shares — vista cross-paziente di tutti i link di
// condivisione che il caller ha creato. Riusa il componente
// ShareLinksTable senza filtro patient_id.

import { useTranslations } from "next-intl";
import Link from "next/link";

import ShareLinksTable from "@/components/ShareLinksTable";

export default function SettingsSharesPage() {
  const t = useTranslations("settingsShares");
  return (
    <main>
      <p className="meta">
        <Link href="/settings">← {t("backToSettings")}</Link>
      </p>
      <h1>{t("title")}</h1>
      <p style={{ marginBottom: "1rem", opacity: 0.8 }}>{t("subtitle")}</p>
      <ShareLinksTable />
    </main>
  );
}
