"use client";

// Patient-visible consent ledger — the append-only grant/revoke history.
//
// Reversible, patient-mediated, provable consent: the governance an
// irreversible data lake precludes by construction. The endpoint derives
// the history from the very rows that gate data use (cohort selection
// filters revoked consents), so what is shown cannot disagree with what
// actually governs processing. GDPR Art. 7(1): demonstrable, point-in-time.
//
// Lives on the privacy page beside the current consent toggles + the
// PHR-Bundle export. Collapsed by default; fetches lazily on first open.

import { useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, type ConsentLedger, gdprApi } from "@/lib/api";

// Friendly i18n label per account consent kind. Unknown kinds fall back to
// the raw kind so a newly added consent still renders (just unlabelled).
const KIND_KEYS: Record<string, string> = {
  terms_of_service: "kindTermsOfService",
  privacy_policy: "kindPrivacyPolicy",
  marketing_email: "kindMarketingEmail",
  research_use: "kindResearchUse",
  commercial_use: "kindCommercialUse",
  ai_training: "kindAiTraining",
  third_party_sharing: "kindThirdPartySharing",
};

export default function ConsentLedgerPanel() {
  const t = useTranslations("consentLedger");
  const [data, setData] = useState<ConsentLedger | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    if (loaded) return;
    setLoaded(true);
    try {
      setData(await gdprApi.consentLedger());
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadError"));
    }
  }

  function whatLabel(ev: ConsentLedger["events"][number]): string {
    if (ev.scope === "account") {
      const key = ev.kind ? KIND_KEYS[ev.kind] : undefined;
      return key ? t(key) : (ev.kind ?? "—");
    }
    // study-scoped training opt-in
    const tier = ev.tier ? ev.tier.toUpperCase() : "";
    const ref = ev.study_id ? ev.study_id.slice(0, 8) : "";
    return t("studyConsent", { tier, study: ref });
  }

  const activeStudies = data?.active_study_consents.length ?? 0;
  const activeAccount = data?.account_consents.filter((c) => c.granted).length ?? 0;

  return (
    <details
      className="card"
      style={{ padding: "0.6rem 0.9rem", marginTop: "2rem" }}
      onToggle={(e) => {
        if ((e.currentTarget as HTMLDetailsElement).open) void load();
      }}
    >
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>{t("title")}</summary>

      {err && (
        <p className="error" style={{ marginTop: "0.6rem" }}>
          {err}
        </p>
      )}

      {!err && !data && (
        <p className="meta" style={{ marginTop: "0.6rem" }} aria-live="polite">
          {t("loading")}
        </p>
      )}

      {data && (
        <div style={{ marginTop: "0.6rem" }}>
          <p className="meta" style={{ marginBottom: "0.5rem" }}>
            {t("summary", { account: activeAccount, studies: activeStudies })}
          </p>

          {data.events.length === 0 ? (
            <p className="meta">{t("noEvents")}</p>
          ) : (
            <table style={{ width: "100%", fontSize: "0.88rem", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={{ textAlign: "left", paddingBottom: "0.25rem" }}>{t("colWhen")}</th>
                  <th style={{ textAlign: "left", paddingBottom: "0.25rem" }}>{t("colEvent")}</th>
                  <th style={{ textAlign: "left", paddingBottom: "0.25rem" }}>{t("colWhat")}</th>
                </tr>
              </thead>
              <tbody>
                {data.events.map((ev, i) => (
                  <tr
                    key={`${ev.at}:${ev.scope}:${ev.kind ?? ev.study_id ?? ""}:${ev.action}:${i}`}
                  >
                    <td style={{ whiteSpace: "nowrap", paddingRight: "0.6rem" }}>
                      {new Date(ev.at).toLocaleString()}
                    </td>
                    <td style={{ paddingRight: "0.6rem" }}>
                      <span
                        style={{
                          fontWeight: 600,
                          color:
                            ev.action === "granted"
                              ? "var(--bv-success, #1a7f37)"
                              : "var(--bv-danger, #b3261e)",
                        }}
                      >
                        {t(ev.action)}
                      </span>
                    </td>
                    <td>
                      {whatLabel(ev)}
                      {ev.consent_version != null && (
                        <span className="meta" style={{ marginLeft: "0.4rem", fontSize: "0.8em" }}>
                          {t("versionLabel", { version: ev.consent_version })}
                        </span>
                      )}
                      {ev.reason && (
                        <span className="meta" style={{ marginLeft: "0.4rem", fontSize: "0.8em" }}>
                          {t("reasonLabel", { reason: ev.reason })}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <p
            className="meta"
            style={{ marginTop: "0.75rem", fontSize: "0.8rem", color: "var(--bv-muted, #888)" }}
          >
            {data.scope}
          </p>
        </div>
      )}
    </details>
  );
}
