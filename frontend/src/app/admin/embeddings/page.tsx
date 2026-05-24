"use client";

// Admin dashboard: embedding coverage for two distinct stacks:
//   1. Imaging (BiomedCLIP 512-d) over study / series / instance.
//   2. Text chunks (multilingual MiniLM 384-d) over every text-bearing
//      source: documents, clinical_notes, summaries, report_contents.
//
// The two are kept visually separate because they live in different
// tables (``embeddings`` vs ``text_embeddings``), are produced by
// different worker tasks, and serve different consumers (semantic
// image search vs Q&A retrieval).

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  type EmbeddingCoverage,
  type EmbeddingCoverageRow,
  type EmbeddingTargetKind,
  type TextChunkCoverage,
  embeddingsAdminApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

type RowKey = `${string}::${EmbeddingTargetKind}`;

function rowKey(row: EmbeddingCoverageRow): RowKey {
  return `${row.model_id}::${row.target_kind}`;
}

export default function EmbeddingsAdminPage() {
  const { user, status } = useAuth();
  const [data, setData] = useState<EmbeddingCoverage | null>(null);
  const [textData, setTextData] = useState<TextChunkCoverage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<RowKey, boolean>>({});
  const [busy, setBusy] = useState<RowKey | null>(null);
  const [textBusy, setTextBusy] = useState(false);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [a, b] = await Promise.all([
        embeddingsAdminApi.coverage(),
        embeddingsAdminApi.textChunkCoverage(),
      ]);
      setData(a);
      setTextData(b);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    if (!user?.is_admin) return;
    load();
  }, [user, load]);

  const onEmbedMissing = async (row: EmbeddingCoverageRow) => {
    const key = rowKey(row);
    setBusy(key);
    setNotice(null);
    setErr(null);
    try {
      const res = await embeddingsAdminApi.embedMissing(row.model_id, row.target_kind);
      setNotice(
        `Enqueued ${res.enqueued} ${row.target_kind}(s) for ${row.model_id} (missing embeddings).`,
      );
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "embed-missing failed");
    } finally {
      setBusy(null);
    }
  };

  const onRetryFailed = async (row: EmbeddingCoverageRow) => {
    const key = rowKey(row);
    setBusy(key);
    setNotice(null);
    setErr(null);
    try {
      const res = await embeddingsAdminApi.retryFailed(row.model_id, row.target_kind);
      setNotice(`Re-enqueued ${res.enqueued} failed ${row.target_kind}(s) for ${row.model_id}.`);
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "retry-failed failed");
    } finally {
      setBusy(null);
    }
  };

  const toggle = (row: EmbeddingCoverageRow) => {
    const key = rowKey(row);
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  if (status === "loading") {
    return (
      <main>
        <p className="meta">Loading...</p>
      </main>
    );
  }

  if (!user) {
    return (
      <main>
        <h1>Embeddings</h1>
        <p className="error">You must be logged in to access this page.</p>
      </main>
    );
  }

  if (!user.is_admin) {
    return (
      <main>
        <h1>Embeddings</h1>
        <p className="error">Admin access required.</p>
      </main>
    );
  }

  const onEmbedMissingTextChunks = async () => {
    setTextBusy(true);
    setNotice(null);
    setErr(null);
    try {
      const res = await embeddingsAdminApi.embedMissingTextChunks();
      setNotice(`Enqueued ${res.enqueued} text chunks for embedding.`);
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "embed-missing failed");
    } finally {
      setTextBusy(false);
    }
  };

  return (
    <main>
      <h1>Embedding coverage</h1>
      <p className="meta" style={{ marginBottom: "1.25rem" }}>
        BitVision Phoenix maintains <strong>due stack di embedding</strong> distinti: immagini
        (BiomedCLIP 512-d) per la ricerca visuale di studi/serie, e testi (MiniLM 384-d) per il
        retrieval Q&amp;A su documenti, note cliniche, riassunti e referti. Le due sezioni qui sotto
        sono indipendenti.
      </p>

      {err && <p className="error">{err}</p>}
      {notice && <p className="meta">{notice}</p>}

      <h2 style={{ marginTop: "1.5rem" }}>📚 Text chunks (Q&amp;A retrieval)</h2>
      <p className="meta" style={{ fontSize: "0.85rem", marginBottom: "0.5rem" }}>
        Vettori 384-d MiniLM consumati da <code>chunk_search</code> e dal flusso
        <code> /api/patients/&lt;id&gt;/ask</code>. I chunk vengono creati dai worker
        <code> chunk_and_embed_*</code> a ogni nuova nota / documento; i vettori possono mancare se
        il worker è partito senza l'extra <code>ai</code> o se il backfill non è stato lanciato.
      </p>
      {!textData && <p className="meta">Caricamento copertura testi…</p>}
      {textData && (
        <div
          className="card"
          style={{
            padding: "0.85rem 1rem",
            marginBottom: "1.5rem",
            background: "var(--bv-card-bg-soft, transparent)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "1rem",
              flexWrap: "wrap",
            }}
          >
            <div style={{ flex: "1 1 18rem" }}>
              <div>
                <strong>{textData.model_id}</strong> <span className="meta">(document_chunk)</span>
              </div>
              <div className="meta" style={{ fontSize: "0.85em" }}>
                {textData.embedded_chunks} / {textData.total_chunks} embedded ·{" "}
                {textData.pending_chunks} pending
              </div>
              <ProgressBar
                percentage={textData.pct}
                total={textData.total_chunks}
                done={textData.embedded_chunks}
                failed={0}
              />
            </div>
            <div>
              <button
                type="button"
                onClick={onEmbedMissingTextChunks}
                disabled={textBusy || textData.pending_chunks === 0}
                title={
                  textData.pending_chunks === 0
                    ? "Tutti i chunk sono già embedded"
                    : `Re-enqueue ${textData.pending_chunks} job di embedding`
                }
              >
                {textBusy ? "Enqueue…" : `Embed missing (${textData.pending_chunks})`}
              </button>
            </div>
          </div>

          {textData.by_source_kind.length > 0 && (
            <table
              style={{
                width: "100%",
                marginTop: "0.75rem",
                fontSize: "0.82rem",
                borderCollapse: "collapse",
              }}
            >
              <thead>
                <tr style={{ textAlign: "left" }}>
                  <th style={{ padding: "0.3rem 0.5rem" }}>Sorgente</th>
                  <th style={{ padding: "0.3rem 0.5rem", textAlign: "right" }}>Embedded</th>
                  <th style={{ padding: "0.3rem 0.5rem", textAlign: "right" }}>Totale</th>
                  <th style={{ padding: "0.3rem 0.5rem", textAlign: "right" }}>Pending</th>
                </tr>
              </thead>
              <tbody>
                {textData.by_source_kind.map((row) => (
                  <tr key={row.source_kind}>
                    <td style={{ padding: "0.25rem 0.5rem" }}>{row.source_kind}</td>
                    <td style={{ padding: "0.25rem 0.5rem", textAlign: "right" }}>
                      {row.embedded}
                    </td>
                    <td style={{ padding: "0.25rem 0.5rem", textAlign: "right" }}>{row.total}</td>
                    <td
                      style={{
                        padding: "0.25rem 0.5rem",
                        textAlign: "right",
                        color: row.pending > 0 ? "var(--bv-warn, #c80)" : "inherit",
                      }}
                    >
                      {row.pending}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <h2 style={{ marginTop: "1.5rem" }}>🩻 Imaging (visual search)</h2>
      <p className="meta" style={{ fontSize: "0.85rem", marginBottom: "0.5rem" }}>
        Vettori BiomedCLIP 512-d sulle DICOM (study / series / instance). Usato dalla ricerca
        visuale e dal &ldquo;similar series&rdquo; tool MCP. &ldquo;Embed all missing&rdquo; subito
        dopo aver attivato un nuovo modello; &ldquo;Retry failed&rdquo; dopo aver risolto un'ondata
        di errori (es. CUDA OOM).
      </p>

      {!data && <p className="meta">Caricamento copertura immagini…</p>}
      {data && data.items.length === 0 && (
        <p className="meta">
          Nessun embedding immagine ancora prodotto. Apri uno studio e clicca &ldquo;embed
          series&rdquo; oppure attendi il job di registration che parte automaticamente all'upload.
        </p>
      )}

      {data && data.items.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          {data.items.map((row) => {
            const key = rowKey(row);
            const isBusy = busy === key;
            const isOpen = !!expanded[key];
            return (
              <div
                key={key}
                style={{
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: "0.75rem 1rem",
                  marginBottom: "0.75rem",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "1rem",
                    flexWrap: "wrap",
                  }}
                >
                  <div style={{ flex: "1 1 18rem" }}>
                    <div>
                      <strong>{row.model_id}</strong>{" "}
                      <span className="meta">({row.target_kind})</span>
                    </div>
                    <div className="meta" style={{ fontSize: "0.85em" }}>
                      {row.done} / {row.total} done &middot; {row.failed} failed &middot;{" "}
                      {row.pending} pending
                    </div>
                    <ProgressBar
                      percentage={row.percentage}
                      total={row.total}
                      done={row.done}
                      failed={row.failed}
                    />
                  </div>
                  <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                    <button
                      type="button"
                      disabled={isBusy || row.pending === 0}
                      onClick={() => onEmbedMissing(row)}
                      title="Enqueue every target of this kind that has no embedding for this model"
                    >
                      {isBusy ? "Enqueuing..." : `Embed all missing (${row.pending})`}
                    </button>
                    <button
                      type="button"
                      disabled={isBusy || row.failed === 0}
                      onClick={() => onRetryFailed(row)}
                      title="Re-enqueue every target that has an error row and no embedding"
                    >
                      {isBusy ? "Enqueuing..." : `Retry failed (${row.failed})`}
                    </button>
                    <button
                      type="button"
                      onClick={() => toggle(row)}
                      disabled={row.last_failures.length === 0}
                      title={
                        row.last_failures.length === 0
                          ? "No failures recorded"
                          : "Show the 10 most recent failures"
                      }
                    >
                      {isOpen ? "Hide failures" : `Show failures (${row.last_failures.length})`}
                    </button>
                  </div>
                </div>

                {isOpen && row.last_failures.length > 0 && (
                  <div
                    style={{
                      marginTop: "0.75rem",
                      borderTop: "1px solid var(--border)",
                      paddingTop: "0.5rem",
                    }}
                  >
                    <table
                      style={{
                        width: "100%",
                        borderCollapse: "collapse",
                        fontSize: "0.85em",
                      }}
                    >
                      <thead>
                        <tr style={{ textAlign: "left" }}>
                          <th style={{ padding: "0.25rem 0.5rem" }}>Target</th>
                          <th style={{ padding: "0.25rem 0.5rem" }}>Class</th>
                          <th style={{ padding: "0.25rem 0.5rem" }}>Message</th>
                          <th style={{ padding: "0.25rem 0.5rem" }}>Retries</th>
                          <th style={{ padding: "0.25rem 0.5rem" }}>When</th>
                        </tr>
                      </thead>
                      <tbody>
                        {row.last_failures.map((f) => (
                          <tr key={`${f.target_id}-${f.failed_at}`}>
                            <td
                              style={{
                                padding: "0.25rem 0.5rem",
                                fontFamily: "monospace",
                                fontSize: "0.8em",
                              }}
                              title={f.target_id}
                            >
                              {f.target_id.slice(0, 8)}&hellip;
                            </td>
                            <td style={{ padding: "0.25rem 0.5rem" }}>{f.error_class ?? "-"}</td>
                            <td
                              style={{
                                padding: "0.25rem 0.5rem",
                                maxWidth: "32rem",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                              title={f.error_message}
                            >
                              {f.error_message}
                            </td>
                            <td style={{ padding: "0.25rem 0.5rem" }}>{f.retry_count}</td>
                            <td style={{ padding: "0.25rem 0.5rem" }}>
                              {new Date(f.failed_at).toLocaleString()}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </main>
  );
}

function ProgressBar({
  percentage,
  total,
  done,
  failed,
}: {
  percentage: number;
  total: number;
  done: number;
  failed: number;
}) {
  // Split the bar into three: done (green), failed (red), pending (grey).
  // Done+failed+pending might not sum to total for some rows if the same
  // target has both an embedding and a stale error row — clamp so widths
  // always stay within the bar.
  const donePct = total > 0 ? Math.min((done / total) * 100, 100) : 0;
  const failedPct = total > 0 ? Math.min((failed / total) * 100, Math.max(100 - donePct, 0)) : 0;

  return (
    <div
      style={{
        display: "flex",
        height: 10,
        borderRadius: 5,
        overflow: "hidden",
        background: "var(--border, #e5e7eb)",
        marginTop: "0.35rem",
        width: "100%",
        maxWidth: 360,
      }}
      aria-label={`Progress: ${percentage.toFixed(1)}% done`}
    >
      <div style={{ width: `${donePct}%`, background: "#16a34a" }} />
      <div style={{ width: `${failedPct}%`, background: "#dc2626" }} />
    </div>
  );
}
