"use client";

// Per-study text de-identification provenance panel (OpenData transparency).
//
// The auditable counterpart to an irreversible black-box: it shows what
// was redacted from a public study's clinical notes at publication —
// category counts, the LLM model/provider when an LLM scrub ran, and the
// contribution tier. Aggregate + storage-isolated (the endpoint returns
// only counts; no note id, excerpt, actor, or S3 location).
//
// Renders next to the "Public Dataset · CC-BY · cite" badge on the study
// page. The caller gates on ``study.is_public`` (the endpoint is
// study-detail gated). Collapsed by default; fetches lazily on first open
// so a closed panel costs nothing.
//
// GUARD-RAIL (load-bearing): this is TEXT de-identification only. The
// ``scope`` line makes explicit it does NOT cover DICOM header/pixel
// de-id (PS3.15) — never frame it as irreversible-anonymisation parity.

import { useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, type DeidentificationProvenance, studiesApi } from "@/lib/api";

// Friendly i18n label per backend redaction_kind. Unknown kinds fall back
// to the raw category so a new redaction pass still renders (just unlabelled).
const CATEGORY_KEYS: Record<string, string> = {
  regex_codice_fiscale: "catCodiceFiscale",
  regex_email: "catEmail",
  regex_phone: "catPhone",
  regex_date_precise: "catDatePrecise",
  regex_address: "catAddress",
  llm_scrub_via_mcp: "catLlmScrub",
  manual: "catManual",
};

export default function DeidentificationProvenancePanel({ studyId }: { studyId: string }) {
  const t = useTranslations("deidentificationProvenance");
  const [data, setData] = useState<DeidentificationProvenance | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    if (loaded) return;
    setLoaded(true);
    try {
      setData(await studiesApi.deidentificationProvenance(studyId));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadError"));
    }
  }

  function categoryLabel(category: string): string {
    const key = CATEGORY_KEYS[category];
    return key ? t(key) : category;
  }

  return (
    <details
      className="card"
      style={{ padding: "0.6rem 0.9rem", marginBottom: "1.25rem" }}
      onToggle={(e) => {
        if ((e.currentTarget as HTMLDetailsElement).open) void load();
      }}
    >
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>{t("title")}</summary>

      {err && (
        <p className="error" style={{ marginTop: "0.6rem" }}>
          {err}
        </p>
      )}

      {!err && !data && (
        <p className="meta" style={{ marginTop: "0.6rem" }} aria-live="polite">
          {t("loading")}
        </p>
      )}

      {data && (
        <div style={{ marginTop: "0.6rem" }}>
          {data.total_text_redactions === 0 ? (
            <p className="meta">{t("noneRedacted")}</p>
          ) : (
            <>
              <p className="meta" style={{ marginBottom: "0.5rem" }}>
                {t("summary", {
                  total: data.total_text_redactions,
                  notes: data.notes_redacted,
                })}
              </p>
              <table style={{ width: "100%", fontSize: "0.88rem", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: "left", paddingBottom: "0.25rem" }}>
                      {t("colCategory")}
                    </th>
                    <th style={{ textAlign: "right", paddingBottom: "0.25rem" }}>
                      {t("colCount")}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.text_redactions.map((r) => (
                    <tr key={`${r.category}:${r.model_id ?? ""}`}>
                      <td>
                        {categoryLabel(r.category)}
                        {r.model_id && (
                          <span
                            className="meta"
                            style={{ marginLeft: "0.4rem", fontSize: "0.8em" }}
                          >
                            ({r.provider ? `${r.provider} · ` : ""}
                            {r.model_id})
                          </span>
                        )}
                      </td>
                      <td style={{ textAlign: "right" }}>{r.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <p
            className="meta"
            style={{ marginTop: "0.75rem", fontSize: "0.8rem", color: "var(--bv-muted, #888)" }}
          >
            {data.scope}
          </p>
        </div>
      )}
    </details>
  );
}
