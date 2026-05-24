"use client";

// Care-phase detail body: header (name, range, narrative) plus three
// sub-tabs (studi / documenti / annotazioni). Hydrated from
// ``GET /care-phases/{id}/material``. Mutations (re-propose, edit)
// gate on ``/api/me/scopes``; the buttons render disabled when the
// caller lacks the scope.
//
// The legacy "Report / Consulti" tab was dropped: those rows backed
// onto a 404 frontend route, and the underlying ReportContents are
// already reachable via the clinical-event detail page that the
// timeline event chip opens.

import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import Markdown from "@/components/Markdown";
import { request } from "@/lib/api";
import {
  type CarePhase,
  type CarePhaseMaterial,
  type MaterialItem,
  carePhasesApi,
} from "@/lib/api_records";

type Tab = "studies" | "documents" | "annotations";

interface MeScopes {
  scopes: string[];
}

export default function CarePhaseDetailBody() {
  const router = useRouter();
  const params = useParams<{ id: string; slug: string }>();
  const patientId = params.id;
  const slug = params.slug;
  const t = useTranslations("carePhaseSlug");
  const locale = useLocale();

  const [phase, setPhase] = useState<CarePhase | null>(null);
  const [material, setMaterial] = useState<CarePhaseMaterial | null>(null);
  const [scopes, setScopes] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("studies");
  const [reproposing, setReproposing] = useState(false);

  // Resolve slug → phase id by listing phases (cheap; backend already
  // indexes (patient_id, slug) UNIQUE). The dedicated GET-by-slug
  // endpoint is intentionally absent in the spec.
  const refresh = useCallback(async () => {
    setError(null);
    try {
      const phases = await carePhasesApi.list(patientId);
      const found = phases.find((p) => p.slug === slug);
      if (!found) {
        setError(t("errorPhaseNotFound"));
        return;
      }
      setPhase(found);
      const m = await carePhasesApi.material(patientId, found.id);
      setMaterial(m);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("errorLoad"));
    }
  }, [patientId, slug, t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    let cancelled = false;
    request<MeScopes>("/api/me/scopes")
      .then((r) => {
        if (!cancelled) setScopes(r.scopes ?? []);
      })
      .catch(() => !cancelled && setScopes([]));
    return () => {
      cancelled = true;
    };
  }, []);

  const canWrite = scopes?.includes("phases:write") ?? false;
  const canPropose = canWrite || (scopes?.includes("phases:propose") ?? false);

  async function handleRepropose() {
    if (reproposing) return;
    setReproposing(true);
    try {
      await carePhasesApi.propose(patientId, { lang: locale });
      router.push(`/patients/${patientId}?view=events`);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("errorPropose"));
    } finally {
      setReproposing(false);
    }
  }

  if (error) {
    return (
      <main>
        <p className="error">{error}</p>
      </main>
    );
  }
  if (!phase || !material) {
    return (
      <main>
        <p className="meta">{t("loading")}</p>
      </main>
    );
  }

  const range =
    phase.start_date || phase.end_date
      ? `${phase.start_date ?? "?"} → ${phase.end_date ?? t("rangeOpen")}`
      : t("rangeUnknown");

  return (
    <main>
      <p className="meta">
        <Link href={`/patients/${patientId}?view=events`}>{t("backToTimeline")}</Link>
      </p>

      <header
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: "0.75rem",
          marginBottom: "1rem",
          paddingBottom: "0.75rem",
          borderBottom: `4px solid ${phase.color_hex}`,
        }}
      >
        <span
          aria-hidden
          style={{
            width: 14,
            height: 36,
            background: phase.color_hex,
            borderRadius: 4,
            marginTop: 6,
          }}
        />
        <div style={{ flex: 1 }}>
          <h1 style={{ margin: "0 0 0.25rem" }}>{phase.name}</h1>
          <p className="meta" style={{ margin: 0 }}>
            {t("headerMeta", {
              range,
              kind: phase.kind,
              events: phase.counts.n_events,
            })}
          </p>
        </div>
        <div style={{ display: "inline-flex", gap: "0.4rem" }}>
          <button
            type="button"
            className="ghost"
            disabled={!canWrite}
            title={canWrite ? t("editPhaseTitle") : t("editPhasePermission")}
            onClick={() => router.push(`/patients/${patientId}?view=events`)}
          >
            {t("editPhase")}
          </button>
          <button
            type="button"
            disabled={!canPropose || reproposing}
            onClick={handleRepropose}
            title={canPropose ? t("reproposeTitle") : t("reproposePermission")}
          >
            {reproposing ? t("reproposeBusy") : t("repropose")}
          </button>
        </div>
      </header>

      {phase.narrative_md && (
        <section
          style={{
            padding: "0.75rem 1rem",
            background: "var(--bv-card-bg)",
            border: "1px solid var(--bv-card-border)",
            borderRadius: 8,
            marginBottom: "1rem",
          }}
        >
          <Markdown text={phase.narrative_md} />
        </section>
      )}

      <div
        role="tablist"
        style={{
          display: "flex",
          gap: "0.2rem",
          borderBottom: "1px solid var(--bv-card-border)",
          marginBottom: "1rem",
        }}
      >
        <TabBtn
          label={t("studiesLabel", { n: material.studies.length })}
          active={tab === "studies"}
          onClick={() => setTab("studies")}
        />
        <TabBtn
          label={t("documentsLabel", { n: material.documents.length })}
          active={tab === "documents"}
          onClick={() => setTab("documents")}
        />
        <TabBtn
          label={t("annotationsLabel", { n: material.annotations.length })}
          active={tab === "annotations"}
          onClick={() => setTab("annotations")}
        />
      </div>

      {tab === "studies" && (
        <MaterialList
          items={material.studies}
          empty={t("emptyStudies")}
          phaseSlug={slug}
          phaseName={phase.name}
        />
      )}
      {tab === "documents" && (
        <MaterialList
          items={material.documents}
          empty={t("emptyDocuments")}
          phaseSlug={slug}
          phaseName={phase.name}
        />
      )}
      {tab === "annotations" && (
        <MaterialList
          items={material.annotations}
          empty={t("emptyAnnotations")}
          phaseSlug={slug}
          phaseName={phase.name}
        />
      )}
    </main>
  );
}

function appendCarePhaseBack(rawUrl: string, slug: string, name: string): string {
  // Append ``?from=care-phase&phase=<slug>&phaseName=<encoded>`` to
  // every outbound MaterialItem URL so the destination page (study,
  // document, clinical-event) can render a back-link to this phase
  // in addition to its native folder / root chain. Preserves any
  // existing query string and the optional ``#hash`` (the report
  // URL carries ``#rc-<id>`` to scroll the right card into view on
  // the event detail page).
  const [pathPart, hashPart] = rawUrl.split("#");
  const sep = pathPart.includes("?") ? "&" : "?";
  const qs = `from=care-phase&phase=${encodeURIComponent(slug)}&phaseName=${encodeURIComponent(name)}`;
  const next = `${pathPart}${sep}${qs}`;
  return hashPart ? `${next}#${hashPart}` : next;
}

function TabBtn({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}): ReactNode {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      style={{
        padding: "0.45rem 0.85rem",
        background: "transparent",
        border: "none",
        color: active ? "var(--bv-accent)" : "var(--bv-fg)",
        borderBottom: active ? "2px solid var(--bv-accent)" : "2px solid transparent",
        fontWeight: active ? 600 : 400,
        cursor: "pointer",
      }}
    >
      {label}
    </button>
  );
}

function MaterialList({
  items,
  empty,
  phaseSlug,
  phaseName,
}: {
  items: MaterialItem[];
  empty: string;
  phaseSlug: string;
  phaseName: string;
}) {
  if (items.length === 0) {
    return (
      <p className="meta" style={{ fontSize: "0.85rem" }}>
        {empty}
      </p>
    );
  }
  return (
    <ul style={{ listStyle: "none", padding: 0 }}>
      {items.map((it) => (
        <li
          key={`${it.kind}:${it.id}`}
          style={{
            padding: "0.5rem 0.75rem",
            borderBottom: "1px solid var(--bv-card-border)",
            display: "flex",
            alignItems: "baseline",
            gap: "0.6rem",
          }}
        >
          <span
            style={{
              fontSize: "0.7rem",
              padding: "0.1rem 0.4rem",
              borderRadius: 4,
              background: "var(--bv-card-bg)",
              border: "1px solid var(--bv-card-border)",
              color: "var(--bv-fg-soft)",
              textTransform: "uppercase",
            }}
          >
            {it.kind}
          </span>
          <Link
            href={appendCarePhaseBack(it.url, phaseSlug, phaseName)}
            style={{ flex: 1, color: "var(--bv-fg)" }}
          >
            {it.title}
          </Link>
          {it.event_date && (
            <span className="meta" style={{ fontSize: "0.78rem" }}>
              {it.event_date}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
