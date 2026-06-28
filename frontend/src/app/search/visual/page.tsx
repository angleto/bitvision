"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import SeriesPreview from "@/components/SeriesPreview";
import {
  ApiError,
  type Paginated,
  type Series,
  type SimilarStudy,
  type Study,
  errorCode,
  searchApi,
  studiesApi,
} from "@/lib/api";

const MODALITIES = ["CT", "MR", "XR", "US", "other"] as const;
type ModalityFilter = (typeof MODALITIES)[number] | "";

// Mirror of backend ``services.embeddable.NON_EMBEDDABLE_MODALITIES``:
// these DICOM modalities never carry a diagnostic raster image, so
// BiomedCLIP cannot embed them and ``/similar-to`` answers
// ``study_not_indexed``. Hide such series from the reference picker so a
// user can't pick a dead-end exemplar (the canonical cause of the
// "not indexed" card was selecting a Structured Report / dose record).
const NON_EMBEDDABLE_MODALITIES = new Set([
  "SR",
  "PR",
  "KO",
  "SEG",
  "REG",
  "RTSTRUCT",
  "RTPLAN",
  "RTDOSE",
  "RTRECORD",
  "DOC",
  "AU",
  "ECG",
  "EPS",
  "HD",
  "RESP",
  "FID",
  "PLAN",
  "RWV",
  "STAIN",
  "M3D",
]);

function isEmbeddableModality(modality: string | null | undefined): boolean {
  // Blocklist semantics, matching the backend: null / unknown is let
  // through (a real image with an odd modality must not be hidden).
  if (!modality) return true;
  return !NON_EMBEDDABLE_MODALITIES.has(modality.trim().toUpperCase());
}

/**
 * Visual-search reference: the user's chosen exemplar. Either a whole
 * study (the embedding worker picks the first embedded series under
 * the hood) or a single series (forces the similarity to use exactly
 * that series' embedding — useful when a study mixes modalities or
 * the user wants to anchor on, say, the contrast-enhanced phase).
 */
type Reference = { kind: "study"; study: Study } | { kind: "series"; study: Study; series: Series };

/**
 * Status pill for whether an exemplar carries a BiomedCLIP image vector,
 * i.e. whether ``/similar-to`` can anchor on it. ``indexed == null`` (older
 * backend, or an endpoint that didn't compute it) renders nothing so we
 * never show a misleading state. ``false`` is the dead-end the picker now
 * surfaces up front instead of after a pick.
 */
function IndexBadge({ indexed }: { indexed?: boolean | null }) {
  if (indexed == null) return null;
  return indexed ? (
    <span
      className="badge"
      style={{ background: "#e7f5ec", color: "#1a7f43", borderColor: "#bce3cb" }}
      title="Has an image embedding — can anchor a visual search"
    >
      Indexed
    </span>
  ) : (
    <span
      className="badge"
      style={{ background: "#fff7ef", color: "#9a5b00", borderColor: "#fcd9b3" }}
      title="No image vector yet — can't anchor a visual search until it's embedded"
    >
      Not indexed
    </span>
  );
}

// A reference can only anchor a similar-to search if it has an image vector.
// ``indexed === false`` is an explicit dead-end (embeddable but not embedded
// yet, or non-pixel only); ``true`` / ``undefined`` stay pickable (undefined =
// unknown, so we don't block on a backend that didn't compute the flag).
function isDeadEnd(indexed?: boolean | null): boolean {
  return indexed === false;
}

const NOT_INDEXED_HINT =
  "No image vector yet — open it to inspect, or run the embedding backfill, then retry.";

// Visibility scope of the neighbour results. Pure UX narrowing on top of
// the backend auth filter — it can only restrict what the caller may
// already see, never widen it (see /api/similar-to ``scope``). 'shared' =
// studies visible only via a grant (shared with you, not owned/public).
type VisualScope = "all" | "mine" | "shared" | "public";

export default function VisualSearchPage() {
  const [reference, setReference] = useState<Reference | null>(null);
  const [modality, setModality] = useState<ModalityFilter>("");
  const [scope, setScope] = useState<VisualScope>("all");

  return (
    <main>
      <h1>Visual Search</h1>
      <p className="meta">
        Find cases similar to a reference study or series. Search by DICOM metadata (description,
        modality, body part) to pick the exemplar, then jump into any neighbour&apos;s viewer.
      </p>

      {reference ? (
        <NeighborsView
          reference={reference}
          modality={modality}
          scope={scope}
          onModalityChange={setModality}
          onScopeChange={setScope}
          onReset={() => setReference(null)}
          onUseStudy={() => setReference({ kind: "study", study: reference.study })}
        />
      ) : (
        <ReferencePicker onPick={setReference} />
      )}
    </main>
  );
}

function ReferencePicker({ onPick }: { onPick: (r: Reference) => void }) {
  const [q, setQ] = useState("");
  const [modality, setModality] = useState<ModalityFilter>("");
  const [bodyPart, setBodyPart] = useState("");
  const [data, setData] = useState<Paginated<Study> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Debounce server-side search so we don't fire a request per keystroke.
  // ``searchApi.run`` queries study_description / modality / body_part
  // server-side via the same SQL the listing page uses.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const id = setTimeout(async () => {
      try {
        const resp = await searchApi.run({
          q: q.trim() || undefined,
          modality: modality || undefined,
          body_part: bodyPart.trim() || undefined,
          // Ask the backend to flag which studies actually carry an image
          // vector, so the row can mark/disable dead-end exemplars instead
          // of letting the user discover "not indexed" only after picking.
          include_index_status: true,
          limit: 50,
        });
        if (!cancelled) {
          setData(resp);
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [q, modality, bodyPart]);

  return (
    <>
      <h2>Step 1 — Pick a reference</h2>
      <p className="meta">
        Pick a whole study, or expand a row to choose a single series. The chosen exemplar is what
        neighbours are scored against.
      </p>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.5rem",
          margin: "0.75rem 0 1rem",
        }}
      >
        <input
          type="search"
          placeholder="Description / patient / accession…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: "1 1 240px", minWidth: 200 }}
        />
        <select
          value={modality}
          onChange={(e) => setModality(e.target.value as ModalityFilter)}
          style={{
            font: "inherit",
            padding: "0.4rem 0.6rem",
            border: "1px solid #d0d5dd",
            borderRadius: 6,
          }}
        >
          <option value="">Any modality</option>
          {MODALITIES.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <input
          type="search"
          placeholder="Body part…"
          value={bodyPart}
          onChange={(e) => setBodyPart(e.target.value)}
          style={{ width: 160 }}
        />
      </div>

      {err && <p className="error">{err}</p>}
      {!data && !err && <p className="meta">Loading…</p>}
      {data && data.items.length === 0 && !loading && (
        <p className="meta">
          No studies match. Try removing a filter, or <Link href="/upload">upload a new study</Link>
          .
        </p>
      )}

      {data?.items.map((s) => (
        <ReferenceRow key={s.id} study={s} onPick={onPick} />
      ))}

      {data && data.total > data.items.length && (
        <p className="meta" style={{ marginTop: "0.5rem" }}>
          Showing {data.items.length} of {data.total}. Refine the search to narrow.
        </p>
      )}
    </>
  );
}

function ReferenceRow({
  study,
  onPick,
}: {
  study: Study;
  onPick: (r: Reference) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [seriesList, setSeriesList] = useState<Series[] | null>(null);
  const [seriesErr, setSeriesErr] = useState<string | null>(null);

  useEffect(() => {
    if (!expanded || seriesList !== null) return;
    let cancelled = false;
    studiesApi
      .detail(study.id)
      .then((d) => {
        if (!cancelled) setSeriesList(d.series);
      })
      .catch((e) => {
        if (!cancelled) setSeriesErr(e instanceof ApiError ? e.message : "load failed");
      });
    return () => {
      cancelled = true;
    };
  }, [expanded, seriesList, study.id]);

  // Only image series can anchor a visual search; non-image series (SR,
  // dose reports, segmentations) dead-end on /similar-to, so keep them out
  // of the picker and tell the user how many were hidden.
  const embeddableSeries = seriesList?.filter((s) => isEmbeddableModality(s.modality)) ?? null;
  const hiddenCount = seriesList ? seriesList.length - (embeddableSeries?.length ?? 0) : 0;

  return (
    <div className="card" style={{ padding: "0.75rem 1rem", marginBottom: "0.5rem" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "1rem",
        }}
      >
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-label={expanded ? "Collapse series" : "Expand series"}
          style={{
            background: "transparent",
            border: "none",
            color: "inherit",
            padding: 0,
            font: "inherit",
            textAlign: "left",
            flex: 1,
            minWidth: 0,
            cursor: "pointer",
          }}
        >
          <h3 style={{ margin: 0 }}>
            <span style={{ display: "inline-block", width: "0.9rem" }}>{expanded ? "▾" : "▸"}</span>{" "}
            {study.study_description ?? "(no description)"}
            <span className="badges">
              {study.modalities.map((m) => (
                <span key={m} className="badge">
                  {m}
                </span>
              ))}
            </span>
          </h3>
          <div className="meta" style={{ fontSize: "0.82rem", marginTop: "0.2rem" }}>
            {study.study_date ?? "—"} · UID {study.study_instance_uid.slice(0, 36)}…
          </div>
        </button>
        <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", flexShrink: 0 }}>
          <IndexBadge indexed={study.indexed} />
          {/* Open the study detail page to inspect before anchoring on it.
              New tab so the picker + any in-progress search state survives
              the round-trip (the user typically opens a few candidates,
              then comes back to pick one). */}
          <Link
            href={`/studies/${study.id}`}
            target="_blank"
            rel="noopener noreferrer"
            className="ghost"
            style={{ fontSize: "0.85rem", padding: "0.3rem 0.6rem", textDecoration: "none" }}
            title="Open study details in a new tab"
          >
            Open study
          </Link>
          {/* Disabled on a dead-end exemplar so the user can't pick something
              /similar-to would reject; the badge + tooltip say why. */}
          <button
            type="button"
            onClick={() => onPick({ kind: "study", study })}
            disabled={isDeadEnd(study.indexed)}
            title={isDeadEnd(study.indexed) ? NOT_INDEXED_HINT : undefined}
          >
            Use this study
          </button>
        </div>
      </div>

      {expanded && (
        <div style={{ marginTop: "0.6rem" }}>
          {seriesErr && <p className="error">{seriesErr}</p>}
          {seriesList === null && !seriesErr && <p className="meta">Loading series…</p>}
          {seriesList && seriesList.length === 0 && (
            <p className="meta">No series in this study.</p>
          )}
          {embeddableSeries && embeddableSeries.length > 0 && (
            <div className="series-grid">
              {embeddableSeries.map((s) => (
                <SeriesPickCard
                  key={s.id}
                  series={s}
                  onPick={() => onPick({ kind: "series", study, series: s })}
                />
              ))}
            </div>
          )}
          {embeddableSeries &&
            embeddableSeries.length === 0 &&
            seriesList &&
            seriesList.length > 0 && (
              <p className="meta">
                This study has no image series to anchor a visual search (only reports /
                segmentations). Use &ldquo;Use this study&rdquo;, or pick another reference.
              </p>
            )}
          {hiddenCount > 0 && embeddableSeries && embeddableSeries.length > 0 && (
            <p className="meta" style={{ fontSize: "0.78rem", marginTop: "0.4rem" }}>
              {hiddenCount} non-image series (reports, segmentations) hidden &mdash; they
              can&rsquo;t be visually searched.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function SeriesPickCard({
  series,
  onPick,
}: {
  series: Series;
  onPick: () => void;
}) {
  return (
    <div
      className="card series-card"
      style={{
        display: "flex",
        flexDirection: "column",
        padding: "0.5rem",
      }}
    >
      <SeriesPreview
        seriesId={series.id}
        sliceCount={Math.max(1, series.received_instance_count)}
      />
      <div
        style={{
          marginTop: "0.4rem",
          fontSize: "0.82rem",
          display: "flex",
          alignItems: "center",
          gap: "0.35rem",
          flexWrap: "wrap",
        }}
      >
        <span>
          <strong>{series.modality ?? "?"}</strong>
          {series.series_description ? ` · ${series.series_description}` : ""}
        </span>
        <IndexBadge indexed={series.indexed} />
      </div>
      <div className="meta" style={{ fontSize: "0.75rem" }}>
        #{series.series_number ?? "?"} · {series.received_instance_count} img
      </div>
      <div style={{ display: "flex", gap: "0.3rem", marginTop: "0.4rem" }}>
        {/* Disabled on a not-yet-embedded series so it can't anchor a search
            that /similar-to would reject; "Open viewer" stays available. */}
        <button
          type="button"
          onClick={onPick}
          disabled={isDeadEnd(series.indexed)}
          title={isDeadEnd(series.indexed) ? NOT_INDEXED_HINT : undefined}
          style={{ fontSize: "0.82rem" }}
        >
          Use this series
        </button>
        {/* Open the series in the MPR/2D viewer to inspect the pixels before
            anchoring a visual search on it. New tab to preserve the picker. */}
        <Link
          href={`/viewer/series/${series.id}`}
          target="_blank"
          rel="noopener noreferrer"
          className="ghost"
          style={{ fontSize: "0.8rem", padding: "0.2rem 0.5rem", textDecoration: "none" }}
          title="Open series in viewer (new tab)"
        >
          Open viewer
        </Link>
      </div>
    </div>
  );
}

function NeighborsView({
  reference,
  modality,
  scope,
  onModalityChange,
  onScopeChange,
  onReset,
  onUseStudy,
}: {
  reference: Reference;
  modality: ModalityFilter;
  scope: VisualScope;
  onModalityChange: (m: ModalityFilter) => void;
  onScopeChange: (s: VisualScope) => void;
  onReset: () => void;
  onUseStudy: () => void;
}) {
  const [results, setResults] = useState<SimilarStudy[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [errCode, setErrCode] = useState<string | null>(null);

  // The exemplar id is either a study uuid or a series uuid; the
  // backend resolves both via the same /api/similar-to endpoint.
  const targetId = reference.kind === "series" ? reference.series.id : reference.study.id;

  useEffect(() => {
    let cancelled = false;
    setResults(null);
    setErr(null);
    setErrCode(null);
    searchApi
      .similarTo(targetId, {
        k: 12,
        modality: modality || undefined,
        scope: scope === "all" ? undefined : scope,
      })
      .then((resp) => {
        if (!cancelled) setResults(resp);
      })
      .catch((e) => {
        if (cancelled) return;
        const code = errorCode(e);
        if (code) {
          // Structured signal (e.g. study_not_indexed) -> render the right card.
          setErrCode(code);
          setErr(e instanceof ApiError ? e.message : null);
        } else {
          setErr(e instanceof ApiError ? e.message : "load failed");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [targetId, modality, scope]);

  const referenceLabel =
    reference.kind === "series"
      ? `${reference.study.study_description ?? "(no description)"} · series ${reference.series.modality ?? "?"}${
          reference.series.series_description ? ` "${reference.series.series_description}"` : ""
        }`
      : (reference.study.study_description ?? "(no description)");

  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "1rem",
          flexWrap: "wrap",
          marginBottom: "1rem",
        }}
      >
        <div>
          <h2 style={{ margin: 0 }}>Step 2 — Similar cases</h2>
          <p className="meta" style={{ marginBottom: 0 }}>
            Reference: <strong style={{ color: "#111" }}>{referenceLabel}</strong>
            {reference.study.modalities.length > 0
              ? ` · ${reference.study.modalities.join(", ")}`
              : null}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <label style={{ fontSize: "0.85rem", color: "#444" }}>
            Modality{" "}
            <select
              value={modality}
              onChange={(e) => onModalityChange(e.target.value as ModalityFilter)}
              style={{
                font: "inherit",
                padding: "0.4rem 0.6rem",
                border: "1px solid #d0d5dd",
                borderRadius: 6,
                background: "#fff",
              }}
            >
              <option value="">All</option>
              {MODALITIES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
          <label style={{ fontSize: "0.85rem", color: "#444" }}>
            Scope{" "}
            <select
              value={scope}
              onChange={(e) => onScopeChange(e.target.value as VisualScope)}
              title="Which exams to compare against. Only narrows what you can already see — never widens it."
              style={{
                font: "inherit",
                padding: "0.4rem 0.6rem",
                border: "1px solid #d0d5dd",
                borderRadius: 6,
                background: "#fff",
              }}
            >
              <option value="all">All visible</option>
              <option value="mine">Mine</option>
              <option value="shared">Shared with me</option>
              <option value="public">Public</option>
            </select>
          </label>
          <button type="button" className="ghost" onClick={onReset}>
            Change reference
          </button>
        </div>
      </div>

      {errCode === "study_not_indexed" ? (
        <NotIndexedCard
          onReset={onReset}
          // A single non-image series was picked as the exemplar; the
          // study itself may still have an embeddable series, so offer a
          // one-click retry against the whole study.
          onUseStudy={reference.kind === "series" ? onUseStudy : undefined}
        />
      ) : (
        err && <p className="error">{err}</p>
      )}
      {!results && !err && <p className="meta">Searching…</p>}
      {results && results.length === 0 && (
        <p className="meta">
          No similar cases found.{modality ? " Try removing the modality filter." : ""}
        </p>
      )}

      <div className="series-grid">
        {results?.map((hit) => (
          <NeighborCard key={hit.study.id} hit={hit} />
        ))}
      </div>
    </>
  );
}

function NotIndexedCard({
  onReset,
  onUseStudy,
}: {
  onReset: () => void;
  // Present only when the dead-end exemplar was a single non-image series:
  // offers a one-click retry against the whole study.
  onUseStudy?: () => void;
}) {
  return (
    <div
      className="card"
      style={{
        background: "#fff7ef",
        borderColor: "#fcd9b3",
        padding: "1rem 1.25rem",
      }}
    >
      <strong>
        {onUseStudy
          ? "This series has no image data for visual search."
          : "This study isn’t indexed for visual search yet."}
      </strong>
      <p className="meta" style={{ marginTop: "0.4rem", marginBottom: "0.4rem" }}>
        {onUseStudy
          ? "Visual search compares image pixels, and this series (a Structured Report, dose record, or segmentation) carries none. Search the whole study instead: it anchors on the first image series automatically."
          : "Visual search runs on image embeddings produced by a background worker. This study has no image vector yet. Either it was imported recently and the embedder hasn't reached it (bulk imports are embedded by a backfill pass that can lag the import), or it contains only non-pixel series (Structured Reports, segmentations) that can't be embedded."}
      </p>
      <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
        {onUseStudy && (
          <button type="button" onClick={onUseStudy}>
            Search the whole study
          </button>
        )}
        <button type="button" className="ghost" onClick={onReset}>
          Pick a different reference
        </button>
      </div>
    </div>
  );
}

function NeighborCard({ hit }: { hit: SimilarStudy }) {
  // Fetch the *whole* study detail so the card can show the radiology-
  // relevant summary (N series · M images). The matched series'
  // ``received_instance_count`` is also already in there — no need
  // for a second round-trip to ``/api/series/{id}``.
  const [sliceCount, setSliceCount] = useState(1);
  const [seriesCount, setSeriesCount] = useState<number | null>(null);
  const [instanceCount, setInstanceCount] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    studiesApi
      .detail(hit.study.id)
      .then((d) => {
        if (cancelled) return;
        setSeriesCount(d.series.length);
        const total = d.series.reduce((acc, s) => acc + (s.received_instance_count ?? 0), 0);
        setInstanceCount(total);
        const matched = d.series.find((s) => s.id === hit.matched_series_id);
        if (matched) setSliceCount(Math.max(1, matched.received_instance_count));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [hit.study.id, hit.matched_series_id]);

  return (
    <div
      className="card series-card"
      style={{ display: "flex", flexDirection: "column", padding: "0.5rem" }}
    >
      <Link
        href={`/viewer/series/${hit.matched_series_id}`}
        style={{ display: "block", color: "inherit", textDecoration: "none" }}
        title="Open in viewer (use the browser back button to return to search)"
      >
        <SeriesPreview seriesId={hit.matched_series_id} sliceCount={sliceCount} />
        <div style={{ padding: "0.6rem 0 0" }}>
          <h3 style={{ fontSize: "0.9rem" }}>
            {hit.study.study_description ?? "(no description)"}
          </h3>
          <div className="meta" style={{ fontSize: "0.8rem" }}>
            <span className="badges" style={{ marginLeft: 0 }}>
              {hit.study.modalities.map((m) => (
                <span key={m} className="badge">
                  {m}
                </span>
              ))}
              {hit.study.is_public && <span className="badge badge--public">public</span>}
              <span className="badge" title="Contribution tier">
                tier {hit.study.contribution_tier}
              </span>
              <span className="badge badge--llm">score {hit.score.toFixed(3)}</span>
            </span>
          </div>
          {(seriesCount !== null || instanceCount !== null) && (
            <div
              className="meta"
              style={{ fontSize: "0.75rem", marginTop: "0.2rem", opacity: 0.85 }}
            >
              {seriesCount !== null
                ? `${seriesCount} ${seriesCount === 1 ? "serie" : "serie"}`
                : ""}
              {seriesCount !== null && instanceCount !== null ? " · " : ""}
              {instanceCount !== null
                ? `${instanceCount} ${instanceCount === 1 ? "immagine" : "immagini"}`
                : ""}
            </div>
          )}
        </div>
      </Link>
      <div style={{ display: "flex", gap: "0.3rem", marginTop: "0.4rem" }}>
        <Link
          href={`/viewer/series/${hit.matched_series_id}`}
          className="ghost"
          style={{ fontSize: "0.8rem", padding: "0.2rem 0.5rem", textDecoration: "none" }}
        >
          Open viewer
        </Link>
        <Link
          href={`/studies/${hit.study.id}`}
          className="ghost"
          style={{ fontSize: "0.8rem", padding: "0.2rem 0.5rem", textDecoration: "none" }}
        >
          Study details
        </Link>
      </div>
    </div>
  );
}
