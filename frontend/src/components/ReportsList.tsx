"use client";

import { useTranslations } from "next-intl";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useState } from "react";

import { ApiError, type Report, reportsApi } from "@/lib/api";

interface Props {
  studyId: string;
}

export interface ReportsListHandle {
  refresh: () => Promise<void>;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function textPreview(text: string, max = 200): string {
  const trimmed = text.trim();
  if (trimmed.length <= max) return trimmed;
  return `${trimmed.slice(0, max).trimEnd()}...`;
}

/**
 * Reports list for a study. Versions are shown newest-first (the API already
 * orders by version desc). Exposes an imperative `refresh()` so the parent
 * can re-fetch after a new upload without re-rendering cycles.
 */
const ReportsList = forwardRef<ReportsListHandle, Props>(function ReportsList({ studyId }, ref) {
  const t = useTranslations("reportsList");
  const [reports, setReports] = useState<Report[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    try {
      const data = await reportsApi.list(studyId);
      setReports(data);
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [studyId]);

  useImperativeHandle(ref, () => ({ refresh }), [refresh]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (err) return <p className="error">{err}</p>;
  if (!reports) return <p className="meta">{t("loading")}</p>;
  if (reports.length === 0) return <p className="meta">{t("empty")}</p>;

  return (
    <div>
      {reports.map((r) => {
        const isExpanded = !!expanded[r.id];
        const showToggle = r.text.length > 200;
        return (
          <div key={r.id} className="card" style={{ marginBottom: "0.5rem" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                flexWrap: "wrap",
                gap: "0.5rem",
              }}
            >
              <div>
                <span className="badge">v{r.version}</span>
                {r.file_s3_key && (
                  <span className="badge" style={{ marginLeft: "0.3rem" }}>
                    {t("attachmentBadge")}
                  </span>
                )}
                <span className="meta" style={{ marginLeft: "0.5rem", fontSize: "0.8rem" }}>
                  {r.author_subject_id
                    ? t("authorPrefix", { id: r.author_subject_id.slice(0, 8) })
                    : t("authorUnknown")}
                </span>
              </div>
              <span className="meta" style={{ fontSize: "0.8rem" }}>
                {formatDate(r.created_at)}
              </span>
            </div>
            <p style={{ marginTop: "0.4rem", whiteSpace: "pre-wrap" }}>
              {isExpanded ? r.text : textPreview(r.text)}
              {showToggle && (
                <button
                  type="button"
                  className="ghost"
                  style={{ marginLeft: "0.3rem", fontSize: "0.85rem" }}
                  onClick={() => setExpanded((prev) => ({ ...prev, [r.id]: !isExpanded }))}
                >
                  {isExpanded ? t("showLess") : t("showAll")}
                </button>
              )}
            </p>
            {r.file_s3_key && (
              <div className="meta" style={{ fontSize: "0.8rem", marginTop: "0.2rem" }}>
                {t("fileAttached", { type: r.file_content_type ?? "binary" })} ·{" "}
                <code style={{ fontSize: "0.75rem" }}>{r.file_s3_key}</code>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
});

export default ReportsList;
