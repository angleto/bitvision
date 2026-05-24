"use client";

/**
 * /tags — namespace-grouped browser built on top of GET /api/tags/tree.
 *
 * Tags can come from three sources, badged with a coloured dot:
 *   blue   — manual: written by a user via the in-study TagSelector
 *   amber  — auto: produced by the autotag worker (lexicon + optional LLM)
 *   grey   — imported: lifted from an upstream DICOM payload at ingestion
 *
 * Clicking a tag routes to ``/patients?tag=<namespace>:<value>``, the
 * same filter supported by ``GET /api/patients?tag=…``. The Studies
 * tab no longer exists; cross-patient browsing happens from there.
 */

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import TagBrowser from "@/components/TagBrowser";
import { ApiError, type TagTree, tagsApi } from "@/lib/api";

export default function TagsPage() {
  const t = useTranslations("tagsPage");
  const [tree, setTree] = useState<TagTree | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await tagsApi.tree();
        if (!cancelled) setTree(data);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("errorLoad"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  return (
    <main>
      <h1>{t("title")}</h1>
      <p className="meta" style={{ marginBottom: "0.75rem" }}>
        {t("intro")}
      </p>
      <ProvenanceLegend />
      {err && <p className="error">{err}</p>}
      {!tree && !err && <p className="meta">{t("loading")}</p>}
      {tree && tree.length === 0 && (
        <div className="card" style={{ padding: "1rem" }}>
          <strong>{t("noTagsTitle")}</strong>
          <p className="meta" style={{ marginTop: "0.4rem", marginBottom: 0 }}>
            {t("noTagsBody")}
          </p>
        </div>
      )}
      {tree && tree.length > 0 && <TagBrowser tree={tree} />}
    </main>
  );
}

function ProvenanceLegend() {
  return (
    <div
      className="meta"
      style={{
        display: "inline-flex",
        gap: "0.9rem",
        marginBottom: "1rem",
        fontSize: "0.78rem",
      }}
    >
      <LegendDot color="#0ea5e9" label="manual" />
      <LegendDot color="#f59e0b" label="auto" />
      <LegendDot color="#94a3b8" label="imported" />
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span
        aria-hidden
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
        }}
      />
      {label}
    </span>
  );
}
