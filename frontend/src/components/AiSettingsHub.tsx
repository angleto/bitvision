"use client";

/**
 * AiSettingsHub — single-surface AI configuration.
 *
 * Replaces the previous three-pane spread (``/settings/ai-models``,
 * ``/settings/wallet``, ``/settings/api-keys``, ``/settings/ai-assistants``)
 * with one progressively-disclosed page. Default view is three plan
 * cards + a wallet line; everything else (BYOK, MCP wizard, raw
 * provider settings) lives behind explicit disclosures so the page
 * never looks like a cockpit on first open.
 *
 * Copy is intentionally use-case-driven, not technology-driven: a
 * doctor opening this page never sees "mistral-small / qwen3-235b /
 * markup" — they see "Sintesi rapida / Ragionamento profondo /
 * costo per domanda" and pick.
 */

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import {
  type AiTierStatus,
  ApiError,
  type QnaTier,
  type StorageUsageOut,
  type WalletBalanceOut,
  aiTierApi,
  creditsApi,
  storageApi,
} from "@/lib/api";

const GB = 1024 ** 3;
const MB = 1024 ** 2;

function formatBytes(bytes: number): string {
  if (bytes >= GB) return `${(bytes / GB).toFixed(2)} GB`;
  if (bytes >= MB) return `${(bytes / MB).toFixed(0)} MB`;
  return `${(bytes / 1024).toFixed(0)} KB`;
}

interface TierCard {
  id: QnaTier;
  /** Plain-language one-liner the user understands. */
  title: string;
  /** Cost-in-context: what this typically costs per question. */
  priceLabel: string;
  /** What you can do with this tier. */
  summary: string;
  /** ~3 selling points, no jargon. */
  bullets: string[];
}

const TIER_ORDER: QnaTier[] = ["free", "standard", "premium"];

function buildCards(t: ReturnType<typeof useTranslations>): Record<QnaTier, TierCard> {
  return {
    free: {
      id: "free",
      title: t("free.title"),
      priceLabel: t("free.price"),
      summary: t("free.summary"),
      bullets: [t("free.bullet1"), t("free.bullet2"), t("free.bullet3")],
    },
    standard: {
      id: "standard",
      title: t("standard.title"),
      priceLabel: t("standard.price"),
      summary: t("standard.summary"),
      bullets: [t("standard.bullet1"), t("standard.bullet2"), t("standard.bullet3")],
    },
    premium: {
      id: "premium",
      title: t("premium.title"),
      priceLabel: t("premium.price"),
      summary: t("premium.summary"),
      bullets: [t("premium.bullet1"), t("premium.bullet2"), t("premium.bullet3")],
    },
  };
}

export default function AiSettingsHub() {
  const t = useTranslations("aiSettings");
  const [status, setStatus] = useState<AiTierStatus | null>(null);
  const [balance, setBalance] = useState<WalletBalanceOut | null>(null);
  const [storage, setStorage] = useState<StorageUsageOut | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [externalOpen, setExternalOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, b, st] = await Promise.all([
          aiTierApi.status(),
          creditsApi.balance(),
          storageApi.usage(),
        ]);
        if (!cancelled) {
          setStatus(s);
          setBalance(b);
          setStorage(st);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("error.load"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  async function pick(tier: QnaTier) {
    if (!status?.allow_user_override) return;
    setSaving(true);
    setErr(null);
    try {
      const updated = await aiTierApi.set(tier);
      setStatus(updated);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("error.save"));
    } finally {
      setSaving(false);
    }
  }

  const cards = buildCards(t);
  const selected: QnaTier = status?.user_override ?? status?.workspace_default ?? "standard";
  const locked = !status?.allow_user_override;

  return (
    <main>
      <h1 style={{ marginBottom: "0.25rem" }}>{t("title")}</h1>
      <p className="meta" style={{ marginTop: 0, marginBottom: "1.5rem" }}>
        {t("subtitle")}
      </p>

      {err && <p className="error">{err}</p>}
      {status === null && !err && <p className="meta">{t("loading")}</p>}

      {status && (
        <>
          {locked && (
            <p className="meta" style={{ marginBottom: "1rem" }}>
              {t("lockedNotice", {
                workspaceTier: cards[status.workspace_default].title,
              })}
            </p>
          )}

          {/* Tier cards — primary surface */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
              gap: "1rem",
            }}
          >
            {TIER_ORDER.map((tier) => {
              const card = cards[tier];
              const isActive = selected === tier;
              const isWorkspaceDefault = status.workspace_default === tier;
              return (
                <button
                  type="button"
                  key={tier}
                  className="card"
                  onClick={() => pick(tier)}
                  disabled={locked || saving || isActive}
                  aria-pressed={isActive}
                  style={{
                    textAlign: "left",
                    cursor: locked ? "not-allowed" : isActive ? "default" : "pointer",
                    border: isActive
                      ? "2px solid var(--bv-success, #16a34a)"
                      : "1px solid var(--bv-input-border)",
                    opacity: locked && !isActive ? 0.55 : 1,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "baseline",
                      marginBottom: "0.4rem",
                    }}
                  >
                    <strong style={{ fontSize: "1.05rem" }}>{card.title}</strong>
                    <span className="meta" style={{ fontSize: "0.8rem", whiteSpace: "nowrap" }}>
                      {card.priceLabel}
                    </span>
                  </div>
                  <p className="meta" style={{ margin: "0 0 0.6rem" }}>
                    {card.summary}
                  </p>
                  <ul
                    style={{
                      margin: 0,
                      paddingLeft: "1.1rem",
                      fontSize: "0.85rem",
                      lineHeight: 1.4,
                    }}
                  >
                    {card.bullets.map((b) => (
                      <li key={b}>{b}</li>
                    ))}
                  </ul>
                  <div style={{ marginTop: "0.6rem", fontSize: "0.8rem", minHeight: "1.1rem" }}>
                    {isActive && (
                      <span style={{ color: "var(--bv-success, #16a34a)", fontWeight: 600 }}>
                        ✓ {t("activeBadge")}
                      </span>
                    )}
                    {!isActive && isWorkspaceDefault && (
                      <span className="meta">{t("workspaceDefaultBadge")}</span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>

          {!locked && status.user_override !== null && (
            <p style={{ marginTop: "0.75rem" }}>
              <button
                type="button"
                className="ghost"
                onClick={async () => {
                  setSaving(true);
                  try {
                    setStatus(await aiTierApi.set(null));
                  } catch (e) {
                    setErr(e instanceof ApiError ? e.message : t("error.save"));
                  } finally {
                    setSaving(false);
                  }
                }}
                disabled={saving}
                style={{ fontSize: "0.85rem" }}
              >
                {t("resetToWorkspaceDefault", {
                  workspaceTier: cards[status.workspace_default].title,
                })}
              </button>
            </p>
          )}

          {/* Wallet inline */}
          {balance && (
            <section
              className="card"
              style={{
                marginTop: "1.5rem",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "1rem",
                flexWrap: "wrap",
              }}
            >
              <div>
                <p className="meta" style={{ margin: 0, fontSize: "0.85rem" }}>
                  {t("wallet.label")}
                </p>
                <strong style={{ fontSize: "1.4rem" }}>${balance.balance_usd.toFixed(2)}</strong>
              </div>
              <Link className="button ghost" href="/settings/wallet">
                {t("wallet.manage")}
              </Link>
            </section>
          )}

          {/* Storage usage card */}
          {storage && <StorageCard storage={storage} />}

          {/* External AI assistant — collapsed by default */}
          <section style={{ marginTop: "1.5rem" }}>
            <button
              type="button"
              className="ghost"
              onClick={() => setExternalOpen((v) => !v)}
              aria-expanded={externalOpen}
              style={{ fontWeight: 500, padding: "0.4rem 0" }}
            >
              {externalOpen ? "▾" : "▸"} {t("external.title")}
            </button>
            {externalOpen && (
              <div className="card" style={{ marginTop: "0.5rem", padding: "1rem" }}>
                <p className="meta" style={{ margin: "0 0 0.75rem" }}>
                  {t("external.summary")}
                </p>
                <ul
                  style={{
                    margin: "0 0 1rem 0",
                    paddingLeft: "1.1rem",
                    fontSize: "0.9rem",
                    lineHeight: 1.5,
                  }}
                >
                  <li>{t("external.bullet1")}</li>
                  <li>{t("external.bullet2")}</li>
                  <li>{t("external.bullet3")}</li>
                </ul>
                <Link className="button" href="/settings/ai-assistants">
                  {t("external.cta")}
                </Link>
              </div>
            )}
          </section>

          {/* Advanced — collapsed by default */}
          <section style={{ marginTop: "1rem" }}>
            <button
              type="button"
              className="ghost"
              onClick={() => setAdvancedOpen((v) => !v)}
              aria-expanded={advancedOpen}
              style={{ fontWeight: 500, padding: "0.4rem 0" }}
            >
              {advancedOpen ? "▾" : "▸"} {t("advanced.title")}
            </button>
            {advancedOpen && (
              <div className="card" style={{ marginTop: "0.5rem", padding: "1rem" }}>
                <p className="meta" style={{ margin: "0 0 0.75rem", fontSize: "0.85rem" }}>
                  {t("advanced.summary")}
                </p>
                <ul style={{ margin: 0, paddingLeft: "1.1rem", fontSize: "0.9rem" }}>
                  <li>
                    <Link href="/settings/api-keys">{t("advanced.byok")}</Link>
                    {" — "}
                    <span className="meta">{t("advanced.byokHint")}</span>
                  </li>
                  <li>
                    <Link href="/settings/wallet">{t("advanced.walletHistory")}</Link>
                    {" — "}
                    <span className="meta">{t("advanced.walletHistoryHint")}</span>
                  </li>
                </ul>
              </div>
            )}
          </section>
        </>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Storage usage card
// ---------------------------------------------------------------------------

/**
 * Visual storage-usage card. Shows bytes used / quota with a progress
 * bar; the bar tints amber > 80% and red > 95% so a tight cap is
 * read at a glance. The top-3 patients by bytes hint at where the
 * volume sits — clicking any of them deep-links to the patient page
 * so the user can prune (delete documents) if they are bumping
 * against the cap.
 */
function StorageCard({ storage }: { storage: StorageUsageOut }) {
  const t = useTranslations("aiSettings.storage");

  const pct = Math.min(storage.percent, 100);
  const overQuota = storage.bytes_used > storage.bytes_quota;
  const barColor = overQuota
    ? "var(--bv-danger, #dc2626)"
    : pct >= 95
      ? "var(--bv-danger, #dc2626)"
      : pct >= 80
        ? "var(--bv-warning, #d97706)"
        : "var(--bv-success, #16a34a)";

  return (
    <section className="card" style={{ marginTop: "1rem", padding: "1rem" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: "0.5rem",
          flexWrap: "wrap",
          gap: "0.5rem",
        }}
      >
        <strong>{t("title")}</strong>
        <span className="meta" style={{ fontSize: "0.85rem" }}>
          {formatBytes(storage.bytes_used)} / {formatBytes(storage.bytes_quota)}
          {!storage.is_workspace_default && (
            <span style={{ marginLeft: "0.5rem", color: "var(--bv-success, #16a34a)" }}>
              ✓ {t("customQuotaBadge")}
            </span>
          )}
        </span>
      </div>

      <div
        style={{
          height: "8px",
          width: "100%",
          background: "var(--bv-input-border, #e5e7eb)",
          borderRadius: "4px",
          overflow: "hidden",
          marginBottom: "0.5rem",
        }}
        aria-hidden="true"
      >
        <div
          style={{
            height: "100%",
            width: `${pct}%`,
            background: barColor,
            transition: "width 0.3s ease",
          }}
        />
      </div>

      <p className="meta" style={{ margin: "0 0 0.5rem", fontSize: "0.85rem" }}>
        {overQuota
          ? t("overQuotaNotice")
          : pct >= 80
            ? t("almostFullNotice", { percent: pct.toFixed(0) })
            : t("normalNotice", { percent: pct.toFixed(0) })}
      </p>

      {storage.top_patients.length > 0 && (
        <details style={{ marginTop: "0.5rem", fontSize: "0.85rem" }}>
          <summary style={{ cursor: "pointer", color: "var(--bv-muted-fg, #6b7280)" }}>
            {t("topPatientsLabel")}
          </summary>
          <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.1rem" }}>
            {storage.top_patients.map((p) => (
              <li key={p.patient_id}>
                <Link href={`/patients/${p.patient_id}`}>
                  {p.display_name ?? p.patient_id.slice(0, 8)}
                </Link>
                {" — "}
                <span className="meta">{formatBytes(p.bytes_used)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
