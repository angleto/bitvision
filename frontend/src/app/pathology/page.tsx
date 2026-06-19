"use client";

/*
 * /pathology — public pathology whole-slide-image (WSI) library.
 *
 * Lists the public slides returned by ``GET
 * /api/pathology-slides?public_only=true`` and renders a grid of cards.
 * Each card shows the slide thumbnail (reusing <PathologyThumbnail>),
 * the stain + source collection, a compact <LicenseBadge>, and links to
 * the deep-zoom viewer at ``/pathology/{id}``.
 *
 * Privacy guarantee: the listing endpoint enforces ``public_only`` so
 * only the OpenData / public slides surface; no S3 keys, no PHI in the
 * response. Public slides are served anonymously, so the page works
 * without a session.
 *
 * Pagination is offset/limit "load more": the endpoint returns a plain
 * JSON array, so "there is more" is inferred from a full-page response
 * (``page.length === PAGE_SIZE``).
 */

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import LicenseBadge from "@/components/LicenseBadge";
import PathologyThumbnail from "@/components/PathologyThumbnail";
import { ApiError, type PathologySlide, listPublicPathologySlides } from "@/lib/api";

const PAGE_SIZE = 60;

export default function PathologyLibraryPage() {
  const t = useTranslations("pathologyLibrary");
  const [slides, setSlides] = useState<PathologySlide[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // ``hasMore`` is true while the last fetched page was full; the first
  // short / empty page proves the catalogue is exhausted.
  const [hasMore, setHasMore] = useState(true);

  const load = useCallback(
    async (offset: number, append: boolean) => {
      if (append) setLoadingMore(true);
      else setBusy(true);
      setErr(null);
      try {
        const page = await listPublicPathologySlides({ limit: PAGE_SIZE, offset });
        setHasMore(page.length === PAGE_SIZE);
        setSlides((prev) => (append ? [...prev, ...page] : page));
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("error"));
      } finally {
        if (append) setLoadingMore(false);
        else setBusy(false);
      }
    },
    [t],
  );

  useEffect(() => {
    load(0, false);
  }, [load]);

  const loadMore = useCallback(() => {
    if (busy || loadingMore || !hasMore) return;
    load(slides.length, true);
  }, [busy, loadingMore, hasMore, slides.length, load]);

  return (
    <main style={{ padding: "1.25rem", maxWidth: 1400, margin: "0 auto" }}>
      <header style={{ marginBottom: "1rem" }}>
        <h1 style={{ margin: 0 }}>{t("title")}</h1>
        <p className="meta" style={{ marginTop: "0.4rem", fontSize: "0.9rem" }}>
          {t("intro")}
        </p>
      </header>

      {err && (
        <p style={{ color: "var(--bv-error, #cf6e6e)", margin: "0.5rem 0" }}>
          {err}{" "}
          <button
            type="button"
            onClick={() => load(0, false)}
            style={{
              marginLeft: "0.5rem",
              padding: "0.2rem 0.6rem",
              fontSize: "0.8rem",
              cursor: "pointer",
            }}
          >
            {t("retry")}
          </button>
        </p>
      )}

      {busy && slides.length === 0 && <p className="meta">{t("loading")}</p>}

      {!busy && !err && slides.length === 0 && <p className="meta">{t("empty")}</p>}

      <ul
        style={{
          listStyle: "none",
          margin: 0,
          padding: 0,
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
          gap: "0.85rem",
        }}
      >
        {slides.map((s) => (
          <li key={s.id} className="card" style={{ padding: "0.6rem", display: "block" }}>
            <Link
              href={`/pathology/${s.id}`}
              style={{ textDecoration: "none", color: "inherit", display: "block" }}
              aria-label={t("open")}
            >
              <PathologyThumbnail slideId={s.id} stain={s.stain} height={150} />
              <div style={{ marginTop: "0.5rem" }}>
                <div style={{ fontWeight: 600, fontSize: "0.9rem" }}>
                  {s.stain || t("stainUnknown")}
                </div>
                <div className="meta" style={{ fontSize: "0.78rem", marginTop: "0.15rem" }}>
                  {s.source_collection ? s.source_collection : s.source_format}
                  {s.magnification != null ? ` · ${s.magnification}x` : ""}
                </div>
              </div>
            </Link>
            {s.license_spdx && (
              <div style={{ marginTop: "0.45rem" }}>
                <LicenseBadge
                  study={{
                    license_spdx: s.license_spdx,
                    license_url: s.license_url,
                    citation_text: s.citation_text,
                    citation_required: s.citation_required,
                    source_collection: s.source_collection,
                    commercial_use_allowed: s.commercial_use_allowed,
                  }}
                  commercialUseAllowed={s.commercial_use_allowed}
                />
              </div>
            )}
          </li>
        ))}
      </ul>

      {hasMore && slides.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          <button
            type="button"
            onClick={loadMore}
            disabled={loadingMore}
            style={{
              padding: "0.45rem 1rem",
              fontSize: "0.85rem",
              background: "transparent",
              border: "1px solid var(--bv-card-border)",
              borderRadius: 4,
              cursor: loadingMore ? "default" : "pointer",
              opacity: loadingMore ? 0.6 : 1,
            }}
          >
            {loadingMore ? t("loadingMore") : t("loadMore")}
          </button>
        </div>
      )}
    </main>
  );
}
