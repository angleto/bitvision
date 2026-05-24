"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { API_BASE_URL, ApiError, type SimilarStudy, getStoredToken, searchApi } from "@/lib/api";

interface Props {
  targetId: string;
  k?: number;
  modality?: string;
}

export default function SimilarCasesPanel({ targetId, k = 8, modality }: Props) {
  const [items, setItems] = useState<SimilarStudy[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setErr(null);
    searchApi
      .similarTo(targetId, { k, modality })
      .then((res) => {
        if (!cancelled) setItems(res);
      })
      .catch((e) => {
        if (cancelled) return;
        // 404 = "no embedding yet" — treat as empty, not an error
        if (e instanceof ApiError && e.status === 404) setItems([]);
        else setErr(e instanceof ApiError ? e.message : "load failed");
      });
    return () => {
      cancelled = true;
    };
  }, [targetId, k, modality]);

  if (err)
    return (
      <p className="error" style={{ fontSize: "0.85rem" }}>
        Similar cases: {err}
      </p>
    );

  if (items === null) return <p className="meta">Loading similar cases…</p>;

  if (items.length === 0) return <p className="meta">No similar cases found.</p>;

  return (
    <div className="series-grid">
      {items.map((item) => (
        <SimilarCaseCard key={item.study.id} item={item} />
      ))}
    </div>
  );
}

function SimilarCaseCard({ item }: { item: SimilarStudy }) {
  const { study, score, matched_series_id } = item;
  const [thumbSrc, setThumbSrc] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    fetch(`${API_BASE_URL}/api/series/${matched_series_id}/thumbnail`, {
      credentials: "include",
      headers,
    })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setThumbSrc(objectUrl);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [matched_series_id]);

  const percent = Math.round(score * 100);

  return (
    <Link
      href={`/studies/${study.id}`}
      className="card series-card"
      style={{ display: "block", color: "inherit", padding: "0.75rem" }}
    >
      <div
        style={{
          position: "relative",
          width: "100%",
          aspectRatio: "1",
          background: "#111",
          borderRadius: 6,
          overflow: "hidden",
        }}
      >
        {thumbSrc ? (
          <img
            src={thumbSrc}
            alt={study.study_description ?? "similar case"}
            draggable={false}
            style={{ width: "100%", height: "100%", objectFit: "contain" }}
          />
        ) : (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              color: "#666",
              fontSize: "0.85rem",
            }}
          >
            loading…
          </div>
        )}
        <div
          style={{
            position: "absolute",
            top: 4,
            right: 4,
            fontSize: "0.7rem",
            color: "#fff",
            background: "rgba(30,64,175,0.85)",
            padding: "2px 6px",
            borderRadius: 4,
            fontWeight: 600,
          }}
        >
          {percent}%
        </div>
      </div>
      <div style={{ padding: "0.6rem 0 0" }}>
        <h3 style={{ fontSize: "0.9rem" }}>{study.study_description ?? "(no description)"}</h3>
        <div className="meta" style={{ fontSize: "0.8rem" }}>
          <span className="badges" style={{ marginLeft: 0 }}>
            {study.modalities.map((m) => (
              <span key={m} className="badge">
                {m}
              </span>
            ))}
          </span>{" "}
          {study.study_date ?? "date unknown"}
        </div>
      </div>
    </Link>
  );
}
