"use client";

import { useEffect, useState } from "react";

import {
  API_BASE_URL,
  ApiError,
  type SimilarStudy,
  errorCode,
  getStoredToken,
  searchApi,
} from "@/lib/api";

interface Props {
  seriesId: string;
  k?: number;
}

// `/api/similar-to/{id}` accepts either a series or study id — we pass the
// series id directly so the match uses this exact series's embedding.
export default function SimilarCasesPanelViewer({ seriesId, k = 5 }: Props) {
  const [items, setItems] = useState<SimilarStudy[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setErr(null);
    searchApi
      .similarTo(seriesId, { k })
      .then((rows) => {
        if (!cancelled) setItems(rows);
      })
      .catch((e) => {
        if (cancelled) return;
        // 404 (no embedding) or study_not_indexed (422, async indexing /
        // non-pixel series) -> quiet empty state, not an error in the viewer.
        if (e instanceof ApiError && (e.status === 404 || errorCode(e) === "study_not_indexed")) {
          setItems([]);
        } else {
          setErr(e instanceof ApiError ? e.message : "load failed");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [seriesId, k]);

  return (
    <>
      {err && <p className="error">{err}</p>}
      {items === null && !err && <p className="meta">Loading…</p>}
      {items && items.length === 0 && !err && <p className="meta">No similar cases found.</p>}
      {items && items.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          {items.map((row) => (
            <SimilarCaseRow key={row.matched_series_id} row={row} />
          ))}
        </div>
      )}
    </>
  );
}

function SimilarCaseRow({ row }: { row: SimilarStudy }) {
  const [src, setSrc] = useState<string | null>(null);
  const pct = Math.round(row.score * 100);
  const description = row.study.study_description ?? "(no description)";
  const modality = row.study.modalities?.[0] ?? null;

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    let createdUrl: string | null = null;
    fetch(`${API_BASE_URL}/api/series/${row.matched_series_id}/thumbnail`, {
      credentials: "include",
      headers,
    })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setSrc(createdUrl);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [row.matched_series_id]);

  return (
    <a
      href={`/viewer/series/${row.matched_series_id}`}
      className="card"
      style={{
        display: "flex",
        gap: "0.5rem",
        alignItems: "center",
        padding: "0.4rem",
        textDecoration: "none",
        color: "inherit",
      }}
    >
      <div
        style={{
          width: 56,
          height: 56,
          flexShrink: 0,
          background: "#111",
          borderRadius: 4,
          overflow: "hidden",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {src ? (
          <img
            src={src}
            alt=""
            draggable={false}
            style={{ width: "100%", height: "100%", objectFit: "contain" }}
          />
        ) : (
          <span className="meta" style={{ fontSize: "0.6rem", color: "#555" }}>
            …
          </span>
        )}
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            fontSize: "0.75rem",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={description}
        >
          {description}
        </div>
        <div className="meta" style={{ fontSize: "0.65rem", marginTop: "0.2rem" }}>
          {modality ? `${modality} · ` : ""}
          <span style={{ color: "#9cb9e6" }}>{pct}% match</span>
        </div>
      </div>
    </a>
  );
}
