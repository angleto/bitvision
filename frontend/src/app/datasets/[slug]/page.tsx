"use client";

// /datasets/[slug] — one public collection in full (task d1fc6ef1).
//
// Public detail + citation surface. Renders the aggregate counts, the
// license, the stable PID, the upstream citation (copyable) and download
// links for BibTeX / RIS / DataCite JSON, plus a small sample-study
// preview. Calls GET /api/catalog/collections/{slug} (anonymous OK).

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, type CatalogCollectionDetail, catalogApi } from "@/lib/api";

export default function DatasetDetailPage() {
  const t = useTranslations("datasetCatalog");
  const params = useParams<{ slug: string }>();
  const slug = Array.isArray(params.slug) ? params.slug[0] : params.slug;

  const [data, setData] = useState<CatalogCollectionDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    (async () => {
      try {
        const body = await catalogApi.getCollection(slug);
        if (!cancelled) {
          setData(body);
          setErr(null);
          setNotFound(false);
        }
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          setNotFound(true);
        } else {
          setErr(e instanceof ApiError ? e.message : t("loadError"));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [slug, t]);

  const citation =
    data?.citation_text ?? (data ? `${data.title}. bitvision OpenData. ${data.pid}` : "");

  async function copyCitation() {
    try {
      await navigator.clipboard.writeText(citation);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard denied (insecure context / permissions) — leave the
      // text visible for manual copy; no hard failure.
    }
  }

  return (
    <main>
      <p className="meta" style={{ marginBottom: "0.75rem" }}>
        <Link href="/datasets">{t("back")}</Link>
      </p>

      {notFound && <p className="error">{t("notFound")}</p>}
      {err && <p className="error">{err}</p>}
      {!data && !err && !notFound && <p className="meta">{t("loading")}</p>}

      {data && (
        <>
          <h1 style={{ marginBottom: "0.25rem" }}>{data.title}</h1>
          <p className="meta" style={{ marginBottom: "1.25rem" }}>
            {t("sourceCollection")}: {data.collection} · {t("pid")}: <code>{data.pid}</code>
          </p>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
              gap: "1rem",
            }}
          >
            <section className="card">
              <h3>{data.title}</h3>
              <table style={{ width: "100%", fontSize: "0.9rem" }}>
                <tbody>
                  <tr>
                    <td>{t("subjects")}</td>
                    <td style={{ textAlign: "right" }}>{data.subjects}</td>
                  </tr>
                  <tr>
                    <td>{t("studies")}</td>
                    <td style={{ textAlign: "right" }}>{data.studies}</td>
                  </tr>
                  <tr>
                    <td>{t("series")}</td>
                    <td style={{ textAlign: "right" }}>{data.series}</td>
                  </tr>
                  <tr>
                    <td>{t("images")}</td>
                    <td style={{ textAlign: "right" }}>{data.instances}</td>
                  </tr>
                  {data.first_published_year !== null && (
                    <tr>
                      <td>{t("publishedYear")}</td>
                      <td style={{ textAlign: "right" }}>{data.first_published_year}</td>
                    </tr>
                  )}
                </tbody>
              </table>

              {data.modalities.length > 0 && (
                <p className="meta" style={{ marginTop: "0.75rem" }}>
                  {t("modalities")}: {data.modalities.join(", ")}
                </p>
              )}
              {data.body_parts.length > 0 && (
                <p className="meta">
                  {t("bodyParts")}: {data.body_parts.join(", ")}
                </p>
              )}

              <p style={{ marginTop: "0.75rem" }}>
                {t("license")}:{" "}
                {data.license_url ? (
                  <a href={data.license_url} target="_blank" rel="noopener noreferrer">
                    {data.license_spdx ?? data.license_url}
                  </a>
                ) : (
                  (data.license_spdx ?? "—")
                )}
                {!data.commercial_use_allowed && ` · ${t("nonCommercial")}`}
              </p>
              {data.citation_required && (
                <p className="meta" style={{ marginTop: "0.5rem" }}>
                  {t("citationRequired")}
                </p>
              )}
            </section>

            <section className="card">
              <h3>{t("citeHeading")}</h3>
              <p style={{ fontSize: "0.86rem", lineHeight: 1.4, whiteSpace: "pre-wrap" }}>
                {citation}
              </p>
              <div
                style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.5rem" }}
              >
                <button type="button" onClick={copyCitation}>
                  {copied ? t("copied") : t("copy")}
                </button>
                <a
                  href={catalogApi.citationDownloadUrl(data.slug, "bibtex")}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("downloadBibtex")}
                </a>
                <a
                  href={catalogApi.citationDownloadUrl(data.slug, "ris")}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("downloadRis")}
                </a>
                <a
                  href={catalogApi.citationDownloadUrl(data.slug, "datacite")}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("downloadDatacite")}
                </a>
              </div>
            </section>
          </div>

          {data.sample_studies.length > 0 && (
            <section className="card" style={{ marginTop: "1rem" }}>
              <h3>{t("samplesHeading")}</h3>
              <ul style={{ margin: 0, fontSize: "0.88rem" }}>
                {data.sample_studies.map((s) => (
                  <li key={s.id}>
                    {s.study_description || "—"}
                    {s.study_date ? ` · ${s.study_date}` : ""}
                    {s.modalities.length > 0 ? ` · ${s.modalities.join(", ")}` : ""}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </main>
  );
}
