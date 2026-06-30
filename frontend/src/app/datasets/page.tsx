"use client";

// /datasets — public dataset catalog (task d1fc6ef1).
//
// The browsable, citable commons over the OpenData library. Public by
// design: it calls GET /api/catalog/collections (anonymous OK) and shows
// only aggregate counts + attribution metadata, never per-study PHI.
// Mirrors the /transparency page's plain client-fetch shape.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError, type CatalogList, catalogApi } from "@/lib/api";

export default function DatasetsPage() {
  const t = useTranslations("datasetCatalog");
  const [data, setData] = useState<CatalogList | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await catalogApi.listCollections();
        if (!cancelled) {
          setData(body);
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("loadError"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  return (
    <main>
      <h1>{t("title")}</h1>
      <p className="meta" style={{ marginBottom: "1rem", maxWidth: "60ch" }}>
        {t("intro")}
      </p>

      {err && <p className="error">{err}</p>}
      {!data && !err && <p className="meta">{t("loading")}</p>}

      {data && (
        <>
          <p className="meta" style={{ marginBottom: "1.5rem" }}>
            {t("totals", {
              collections: data.totals.collections,
              subjects: data.totals.subjects,
              studies: data.totals.studies,
              instances: data.totals.instances,
            })}
          </p>

          {data.collections.length === 0 ? (
            <p className="meta">{t("empty")}</p>
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
                gap: "1rem",
              }}
            >
              {data.collections.map((c) => (
                <section className="card" key={c.slug}>
                  <h3 style={{ marginBottom: "0.4rem" }}>{c.title}</h3>
                  <p className="meta" style={{ marginBottom: "0.6rem" }}>
                    {c.subjects} {t("subjects")} · {c.studies} {t("studies")} · {c.series}{" "}
                    {t("series")} · {c.instances} {t("images")}
                  </p>

                  {c.modalities.length > 0 && (
                    <p style={{ display: "flex", flexWrap: "wrap", gap: "0.3rem" }}>
                      {c.modalities.map((m) => (
                        <span
                          key={m}
                          className="meta"
                          style={{
                            border: "1px solid var(--bv-border, #ccc)",
                            borderRadius: "4px",
                            padding: "0.05rem 0.4rem",
                            fontSize: "0.78rem",
                          }}
                        >
                          {m}
                        </span>
                      ))}
                    </p>
                  )}

                  <p className="meta" style={{ marginTop: "0.5rem" }}>
                    {c.license_spdx ?? "—"}
                    {!c.commercial_use_allowed && ` · ${t("nonCommercial")}`}
                  </p>

                  <p style={{ marginTop: "0.75rem" }}>
                    <Link href={`/datasets/${c.slug}`}>{t("view")} →</Link>
                  </p>
                </section>
              ))}
            </div>
          )}

          <p className="meta" style={{ marginTop: "1.5rem" }}>
            {data.generated_at} · v{data.version}
          </p>
        </>
      )}
    </main>
  );
}
