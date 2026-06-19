"use client";

// /contributions/review — admin review queue for studies offered to the
// OpenData library. Accept publishes (irreversible, human-only); reject purges.
// Both require a reason and ride the submission etag (If-Match). Non-admins get
// a 403 from the backend, surfaced as an error here.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/lib/api";
import { type ContributionSubmission, contributionsApi } from "@/lib/contributions_api";

const STATUS_FILTERS = ["needs_review", "blocked", "promoted", "rejected", "failed"];

export default function ContributionsReviewPage(): React.JSX.Element {
  const t = useTranslations("contributionsReview");
  const [rows, setRows] = useState<ContributionSubmission[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("needs_review");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      setRows(await contributionsApi.queue({ status: statusFilter, limit: 200 }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [statusFilter, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const decide = useCallback(
    async (sub: ContributionSubmission, action: "accept" | "reject") => {
      const reason = (reasons[sub.id] || "").trim();
      if (!reason) {
        setErr(t("reasonRequired"));
        return;
      }
      setBusy(sub.id);
      setErr(null);
      try {
        if (action === "accept") await contributionsApi.accept(sub.id, sub.etag, reason);
        else await contributionsApi.reject(sub.id, sub.etag, reason);
        await refresh();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("actionFailed"));
      } finally {
        setBusy(null);
      }
    },
    [reasons, refresh, t],
  );

  const highRisk = (s: ContributionSubmission) =>
    s.instances.filter((i) => i.pixel_phi_risk === "high").length;

  return (
    <main style={{ padding: 24, maxWidth: 1040, margin: "0 auto" }}>
      <h1>{t("title")}</h1>
      <p>{t("subtitle")}</p>

      <label>
        {t("statusFilter")}{" "}
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_FILTERS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>

      {err && (
        <p role="alert" style={{ color: "crimson" }}>
          {err}
        </p>
      )}

      {rows.length === 0 ? (
        <p style={{ marginTop: 16 }}>{t("empty")}</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 16 }}>
          <thead>
            <tr style={{ textAlign: "left" }}>
              <th>{t("colStatus")}</th>
              <th>{t("colTier")}</th>
              <th>{t("colInstances")}</th>
              <th>{t("colVerdict")}</th>
              <th>{t("colHighRisk")}</th>
              <th>{t("colReview")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => {
              const decidable = s.status === "needs_review" || s.status === "blocked";
              return (
                <tr key={s.id} style={{ borderTop: "1px solid #ddd", verticalAlign: "top" }}>
                  <td>{s.status}</td>
                  <td>{s.target_tier}</td>
                  <td>{s.instance_count}</td>
                  <td>{s.auto_verdict ?? "—"}</td>
                  <td style={{ color: highRisk(s) > 0 ? "crimson" : undefined }}>{highRisk(s)}</td>
                  <td>
                    {decidable ? (
                      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                        <textarea
                          aria-label={t("reasonLabel")}
                          placeholder={t("reasonPlaceholder")}
                          value={reasons[s.id] || ""}
                          onChange={(e) => setReasons((r) => ({ ...r, [s.id]: e.target.value }))}
                          rows={2}
                          style={{ width: 240 }}
                        />
                        <div style={{ display: "flex", gap: 8 }}>
                          {/* Accept is enabled only for needs_review (blocked items can only be rejected). */}
                          {s.status === "needs_review" && (
                            <button
                              type="button"
                              disabled={busy === s.id}
                              onClick={() => decide(s, "accept")}
                            >
                              {t("publish")}
                            </button>
                          )}
                          <button
                            type="button"
                            disabled={busy === s.id}
                            onClick={() => decide(s, "reject")}
                          >
                            {t("reject")}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <span>{s.review_note ?? "—"}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}
