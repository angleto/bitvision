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
  searchApi,
  studiesApi,
} from "@/lib/api";

const MODALITIES = ["CT", "MR", "XR", "US", "other"] as const;
type ModalityFilter = (typeof MODALITIES)[number] | "";

/**
 * Visual-search reference: the user's chosen exemplar. Either a whole
 * study (the embedding worker picks the first embedded series under
 * the hood) or a single series (forces the similarity to use exactly
 * that series' embedding — useful when a study mixes modalities or
 * the user wants to anchor on, say, the contrast-enhanced phase).
 */
type Reference = { kind: "study"; study: Study } | { kind: "series"; study: Study; series: Series };

export default function VisualSearchPage() {
  const [reference, setReference] = useState<Reference | null>(null);
  const [modality, setModality] = useState<ModalityFilter>("");

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
          onModalityChange={setModality}
          onReset={() => setReference(null)}
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
        <button type="button" onClick={() => onPick({ kind: "study", study })}>
          Use this study
        </button>
      </div>

      {expanded && (
        <div style={{ marginTop: "0.6rem" }}>
          {seriesErr && <p className="error">{seriesErr}</p>}
          {seriesList === null && !seriesErr && <p className="meta">Loading series…</p>}
          {seriesList && seriesList.length === 0 && (
            <p className="meta">No series in this study.</p>
          )}
          {seriesList && seriesList.length > 0 && (
            <div className="series-grid">
              {seriesList.map((s) => (
                <SeriesPickCard
                  key={s.id}
                  series={s}
                  onPick={() => onPick({ kind: "series", study, series: s })}
                />
              ))}
            </div>
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
      <div style={{ marginTop: "0.4rem", fontSize: "0.82rem" }}>
        <strong>{series.modality ?? "?"}</strong>
        {series.series_description ? ` · ${series.series_description}` : ""}
      </div>
      <div className="meta" style={{ fontSize: "0.75rem" }}>
        #{series.series_number ?? "?"} · {series.received_instance_count} img
      </div>
      <button type="button" onClick={onPick} style={{ marginTop: "0.4rem", fontSize: "0.82rem" }}>
        Use this series
      </button>
    </div>
  );
}

function NeighborsView({
  reference,
  modality,
  onModalityChange,
  onReset,
}: {
  reference: Reference;
  modality: ModalityFilter;
  onModalityChange: (m: ModalityFilter) => void;
  onReset: () => void;
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
      .similarTo(targetId, { k: 12, modality: modality || undefined })
      .then((resp) => {
        if (!cancelled) setResults(resp);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError) {
          const detail = e.detail as { code?: string; message?: string } | string | undefined;
          if (typeof detail === "object" && detail?.code) {
            setErrCode(detail.code);
            setErr(detail.message ?? e.message);
          } else {
            setErr(e.message);
          }
        } else {
          setErr("load failed");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [targetId, modality]);

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
          <button type="button" className="ghost" onClick={onReset}>
            Change reference
          </button>
        </div>
      </div>

      {errCode === "study_not_indexed" ? (
        <NotIndexedCard onReset={onReset} />
      ) : (
        err && <p className="error">{err}</p>
      )}
      {!results && !err && <p className="meta">Searching…</p>}
      {results && results.length === 0 && (
        <p className="meta">No similar cases found. Try removing the modality filter.</p>
      )}

      <div className="series-grid">
        {results?.map((hit) => (
          <NeighborCard key={hit.study.id} hit={hit} />
        ))}
      </div>
    </>
  );
}

function NotIndexedCard({ onReset }: { onReset: () => void }) {
  return (
    <div
      className="card"
      style={{
        background: "#fff7ef",
        borderColor: "#fcd9b3",
        padding: "1rem 1.25rem",
      }}
    >
      <strong>This study isn&rsquo;t indexed for visual search yet.</strong>
      <p className="meta" style={{ marginTop: "0.4rem", marginBottom: "0.4rem" }}>
        Visual search runs on image embeddings produced by a background worker after upload.
        Indexing usually takes a few minutes. If the study is older than that, it may not contain
        pixel data a model can embed (for example, Structured Report-only series).
      </p>
      <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
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
