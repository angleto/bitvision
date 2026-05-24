"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";

import type { ConsultationCitation } from "@/lib/api";

interface Props {
  citations: ConsultationCitation[];
}

function hrefFor(c: ConsultationCitation): string | null {
  switch (c.target_kind) {
    case "study":
      return `/studies/${c.target_id}`;
    case "series":
      return `/series/${c.target_id}`;
    case "report":
      return `/reports/${c.target_id}`;
    case "document":
      return `/documents/${c.target_id}`;
    case "annotation":
      return `/annotations/${c.target_id}`;
    default:
      return null;
  }
}

export default function ConsultationCitationList({ citations }: Props) {
  const t = useTranslations("consultationCitations");
  if (citations.length === 0) {
    return <p className="meta">{t("empty")}</p>;
  }
  return (
    <div>
      {citations.map((c) => {
        const href = hrefFor(c);
        return (
          <div key={c.id} className="card" style={{ marginBottom: "0.5rem" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span className="badge">{c.target_kind}</span>
              {href ? (
                <Link href={href} style={{ fontSize: "0.85rem" }}>
                  {t("openLink", { kind: c.target_kind })}
                </Link>
              ) : (
                <span className="meta" style={{ fontSize: "0.8rem" }}>
                  {c.target_id}
                </span>
              )}
            </div>
            {c.excerpt && (
              <blockquote
                style={{
                  margin: "0.4rem 0 0",
                  padding: "0.4rem 0.6rem",
                  borderLeft: "3px solid var(--color-border, #d1d5db)",
                  fontStyle: "italic",
                  fontSize: "0.9rem",
                }}
              >
                {c.excerpt}
              </blockquote>
            )}
            {c.locator && (
              <div className="meta" style={{ fontSize: "0.75rem", marginTop: "0.3rem" }}>
                {c.locator}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
