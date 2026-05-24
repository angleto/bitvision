"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";

import ProvenanceTimeline from "@/components/ProvenanceTimeline";
import type { ProvenanceTargetKind } from "@/lib/api_records";

const VALID_KINDS: ProvenanceTargetKind[] = [
  "patient",
  "clinical_event",
  "imaging_study",
  "series",
  "report_content",
  "document",
  "document_file",
  "marker",
  "tag",
  "external_identifier",
  "content_document_link",
  "report_content_citation",
];

export default function ProvenancePage() {
  const params = useParams<{ kind: string; id: string }>();
  const t = useTranslations("provenancePage");
  const tLabels = useTranslations("provenanceLabels");
  const validKind = VALID_KINDS.includes(params.kind as ProvenanceTargetKind);
  const kindLabel = (() => {
    if (!validKind) return tLabels("unknownKind");
    try {
      return tLabels(params.kind);
    } catch {
      return params.kind;
    }
  })();

  return (
    <main style={{ padding: "1rem 1.5rem", maxWidth: "900px" }}>
      <nav style={{ marginBottom: "1rem" }}>
        <Link href="/">{t("home")}</Link>
      </nav>

      <h1>{t("title")}</h1>
      <p style={{ color: "var(--muted-fg, #666)" }}>
        {kindLabel} <code style={{ marginLeft: "0.5rem" }}>{params.id}</code>
      </p>
      <p
        style={{
          color: "var(--muted-fg, #666)",
          maxWidth: "60ch",
          fontSize: "0.875rem",
        }}
      >
        {t("intro")}
      </p>

      {!validKind ? (
        <p role="alert" style={{ color: "#c00" }}>
          {t("invalidKind", { kind: params.kind })}
        </p>
      ) : (
        <ProvenanceTimeline targetKind={params.kind as ProvenanceTargetKind} targetId={params.id} />
      )}
    </main>
  );
}
