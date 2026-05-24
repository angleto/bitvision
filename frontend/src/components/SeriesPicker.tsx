"use client";

import { useEffect, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import { ApiError, type Series, type Study, studiesApi } from "@/lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  onPick: (seriesId: string, label: string) => void;
  excludeSeriesIds?: string[];
}

export default function SeriesPicker({ open, onClose, onPick, excludeSeriesIds = [] }: Props) {
  const [studies, setStudies] = useState<Study[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [seriesByStudy, setSeriesByStudy] = useState<Record<string, Series[]>>({});
  const [loadingSeriesFor, setLoadingSeriesFor] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setStudies(null);
    setErr(null);
    studiesApi
      .list({ limit: 30 })
      .then((p) => {
        if (!cancelled) setStudies(p.items);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const expandStudy = async (studyId: string) => {
    if (expandedId === studyId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(studyId);
    if (seriesByStudy[studyId]) return;
    setLoadingSeriesFor(studyId);
    try {
      const detail = await studiesApi.detail(studyId);
      setSeriesByStudy((prev) => ({ ...prev, [studyId]: detail.series }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    } finally {
      setLoadingSeriesFor(null);
    }
  };

  if (!open) return null;

  return (
    <NativeDialog open={open} onClose={onClose} className="bv-dialog">
      <div
        style={{
          width: "min(640px, 90vw)",
          maxHeight: "80vh",
          overflow: "auto",
          background: "#0f131c",
          border: "1px solid #2a2f3b",
          borderRadius: 6,
          padding: "1rem",
          color: "#ddd",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h2 style={{ margin: 0, fontSize: "1rem" }}>Pick a series</h2>
          <button
            type="button"
            className="viewer-btn"
            onClick={onClose}
            style={{ fontSize: "0.75rem" }}
          >
            Close
          </button>
        </div>

        {err && <p style={{ color: "#f88" }}>{err}</p>}
        {!studies && !err && <p className="meta">Loading…</p>}
        {studies && studies.length === 0 && (
          <p className="meta">No recent studies. Upload or load a study first.</p>
        )}

        <div style={{ marginTop: "0.75rem", display: "flex", flexDirection: "column", gap: 4 }}>
          {studies?.map((s) => (
            <div key={s.id} style={{ border: "1px solid #2a2f3b", borderRadius: 4 }}>
              <button
                type="button"
                onClick={() => expandStudy(s.id)}
                style={{
                  width: "100%",
                  textAlign: "left",
                  padding: "0.5rem 0.75rem",
                  background: expandedId === s.id ? "#1a2030" : "transparent",
                  color: "#ddd",
                  border: "none",
                  cursor: "pointer",
                }}
              >
                <div style={{ fontSize: "0.85rem" }}>
                  {s.study_description ?? "(no description)"}
                </div>
                <div className="meta" style={{ fontSize: "0.7rem" }}>
                  {s.study_date ?? "—"} · {s.modalities.join(", ") || "?"}
                </div>
              </button>
              {expandedId === s.id && (
                <div style={{ padding: "0.25rem 0.5rem 0.5rem 0.5rem" }}>
                  {loadingSeriesFor === s.id && <p className="meta">Loading series…</p>}
                  {seriesByStudy[s.id]?.map((ser) => {
                    const disabled = excludeSeriesIds.includes(ser.id);
                    const label = `${ser.modality ?? "?"} · ${ser.series_description ?? `series #${ser.series_number ?? "?"}`}`;
                    return (
                      <button
                        key={ser.id}
                        type="button"
                        disabled={disabled}
                        onClick={() => {
                          onPick(ser.id, label);
                          onClose();
                        }}
                        style={{
                          display: "block",
                          width: "100%",
                          textAlign: "left",
                          padding: "0.3rem 0.5rem",
                          marginTop: 2,
                          background: disabled ? "#1a1a1a" : "#12161f",
                          color: disabled ? "#555" : "#cde",
                          border: "1px solid #2a2f3b",
                          borderRadius: 3,
                          cursor: disabled ? "not-allowed" : "pointer",
                          fontSize: "0.75rem",
                        }}
                        title={disabled ? "Already in comparison" : "Add to comparison"}
                      >
                        {label}
                        {disabled ? " · (already added)" : ""}
                      </button>
                    );
                  })}
                  {seriesByStudy[s.id]?.length === 0 && (
                    <p className="meta" style={{ fontSize: "0.7rem" }}>
                      No series in this study.
                    </p>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </NativeDialog>
  );
}
