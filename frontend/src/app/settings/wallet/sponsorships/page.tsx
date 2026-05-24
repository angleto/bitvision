"use client";

/*
 * /settings/wallet/sponsorships — manage cross-subject billing
 * authorisations.
 *
 * Two tabs:
 *
 * - Emesse: rows where the current user is the sponsor (their wallet
 *   pays). Allowed actions: change cap, revoke.
 *
 * - Ricevute: rows where the current user is the sponsored (someone
 *   else pays for their AI calls). Read-only — only the sponsor may
 *   modify or revoke.
 *
 * "New sponsorship" form is under the Emesse tab. The cap input is a
 * free numeric field validated against the workspace ceiling fetched
 * via ``GET /defaults``; quick presets (€2/€5/€10/€25) live next to it.
 */

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  type SponsorshipCreateIn,
  type SponsorshipDefaultsOut,
  type SponsorshipOut,
  type SponsorshipScopeKind,
  sponsorshipsApi,
} from "@/lib/api";

const CAP_PRESETS_CENTS: number[] = [200, 500, 1000, 2500];

function formatEur(cents: number): string {
  return `€${(cents / 100).toFixed(2)}`;
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function SponsorshipsPage() {
  const t = useTranslations("sponsorships");
  const [tab, setTab] = useState<"emitted" | "received">("emitted");
  const [defaults, setDefaults] = useState<SponsorshipDefaultsOut | null>(null);
  const [emitted, setEmitted] = useState<SponsorshipOut[]>([]);
  const [received, setReceived] = useState<SponsorshipOut[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    setErr(null);
    try {
      const [defs, em, rc] = await Promise.all([
        sponsorshipsApi.defaults(),
        sponsorshipsApi.emitted(false),
        sponsorshipsApi.received(false),
      ]);
      setDefaults(defs);
      setEmitted(em);
      setReceived(rc);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr(null);
      try {
        const [defs, em, rc] = await Promise.all([
          sponsorshipsApi.defaults(),
          sponsorshipsApi.emitted(false),
          sponsorshipsApi.received(false),
        ]);
        if (cancelled) return;
        setDefaults(defs);
        setEmitted(em);
        setReceived(rc);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main>
      <h1>{t("title")}</h1>
      <p className="meta" style={{ marginBottom: "1rem" }}>
        {t("intro")}
      </p>

      {err && <p className="error">{err}</p>}

      <nav
        style={{
          display: "flex",
          gap: "0.5rem",
          borderBottom: "1px solid var(--bv-input-border, #ddd)",
          marginBottom: "1rem",
        }}
        aria-label={t("tabs.aria")}
      >
        <button
          type="button"
          className={tab === "emitted" ? "button" : "ghost"}
          onClick={() => setTab("emitted")}
          aria-pressed={tab === "emitted"}
        >
          {t("tabs.emitted")} ({emitted.length})
        </button>
        <button
          type="button"
          className={tab === "received" ? "button" : "ghost"}
          onClick={() => setTab("received")}
          aria-pressed={tab === "received"}
        >
          {t("tabs.received")} ({received.length})
        </button>
      </nav>

      {tab === "emitted" && (
        <>
          {defaults && <NewSponsorshipForm defaults={defaults} onCreated={refresh} />}
          <SponsorshipList rows={emitted} mode="emitted" defaults={defaults} onChange={refresh} />
        </>
      )}

      {tab === "received" && (
        <SponsorshipList rows={received} mode="received" defaults={null} onChange={refresh} />
      )}

      {loading && <p className="meta">{t("loading")}</p>}
    </main>
  );
}

interface NewSponsorshipFormProps {
  defaults: SponsorshipDefaultsOut;
  onCreated: () => Promise<void>;
}

function NewSponsorshipForm({ defaults, onCreated }: NewSponsorshipFormProps) {
  const t = useTranslations("sponsorships");
  const [open, setOpen] = useState(false);
  const [sponsoredId, setSponsoredId] = useState("");
  const [scopeKind, setScopeKind] = useState<SponsorshipScopeKind>("patient");
  const [scopeId, setScopeId] = useState("");
  const [capCents, setCapCents] = useState<number>(defaults.default_cap_cents);
  const [purpose, setPurpose] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const ceiling = defaults.ceiling_cents;
  const overCeiling = ceiling !== null && capCents > ceiling;

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setErr(null);
    if (!sponsoredId.trim()) {
      setErr(t("form.errors.sponsoredRequired"));
      return;
    }
    if (scopeKind !== "global" && !scopeId.trim()) {
      setErr(t("form.errors.scopeIdRequired"));
      return;
    }
    if (capCents <= 0) {
      setErr(t("form.errors.capPositive"));
      return;
    }
    if (overCeiling) {
      setErr(t("form.errors.capOverCeiling", { ceiling: formatEur(ceiling ?? 0) }));
      return;
    }

    setSubmitting(true);
    try {
      const body: SponsorshipCreateIn = {
        sponsored_subject_id: sponsoredId.trim(),
        scope_kind: scopeKind,
        scope_id: scopeKind === "global" ? null : scopeId.trim(),
        cap_cents: capCents,
        purpose: purpose.trim() || undefined,
      };
      await sponsorshipsApi.create(body);
      setSponsoredId("");
      setScopeId("");
      setPurpose("");
      setCapCents(defaults.default_cap_cents);
      setOpen(false);
      await onCreated();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "create failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="card" style={{ marginBottom: "1rem", padding: "1rem" }}>
      <button
        type="button"
        className="ghost"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{ fontWeight: 500 }}
      >
        {open ? "▾" : "▸"} {t("form.title")}
      </button>

      {open && (
        <form onSubmit={submit} style={{ marginTop: "0.75rem" }}>
          <div style={{ display: "grid", gap: "0.75rem" }}>
            <label>
              <span className="meta">{t("form.sponsoredLabel")}</span>
              <input
                type="text"
                value={sponsoredId}
                onChange={(e) => setSponsoredId(e.target.value)}
                placeholder={t("form.sponsoredPlaceholder")}
                style={{ width: "100%" }}
                required
              />
            </label>

            <label>
              <span className="meta">{t("form.scopeKindLabel")}</span>
              <select
                value={scopeKind}
                onChange={(e) => setScopeKind(e.target.value as SponsorshipScopeKind)}
                style={{ width: "100%" }}
              >
                {defaults.scope_kinds.map((k) => (
                  <option key={k} value={k}>
                    {t(`scopeKinds.${k}` as never)}
                  </option>
                ))}
              </select>
            </label>

            {scopeKind !== "global" && (
              <label>
                <span className="meta">{t("form.scopeIdLabel")}</span>
                <input
                  type="text"
                  value={scopeId}
                  onChange={(e) => setScopeId(e.target.value)}
                  placeholder={t("form.scopeIdPlaceholder")}
                  style={{ width: "100%" }}
                  required
                />
              </label>
            )}

            <fieldset
              style={{
                border: "1px solid var(--bv-input-border, #ddd)",
                borderRadius: 4,
                padding: "0.5rem",
              }}
            >
              <legend className="meta" style={{ padding: "0 0.4rem" }}>
                {t("form.capLabel")}
              </legend>
              <div
                style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", marginBottom: "0.5rem" }}
              >
                {CAP_PRESETS_CENTS.map((c) => (
                  <button
                    key={c}
                    type="button"
                    className={capCents === c ? "button" : "ghost"}
                    onClick={() => setCapCents(c)}
                    aria-pressed={capCents === c}
                  >
                    {formatEur(c)}
                  </button>
                ))}
              </div>
              <label style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                <span className="meta">{t("form.capCustomLabel")}</span>
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={(capCents / 100).toFixed(2)}
                  onChange={(e) => {
                    const eur = Number.parseFloat(e.target.value);
                    if (!Number.isNaN(eur)) setCapCents(Math.max(1, Math.round(eur * 100)));
                  }}
                  style={{ width: "8rem" }}
                />
                <span className="meta">€</span>
              </label>
              {ceiling !== null && (
                <p className="meta" style={{ margin: "0.4rem 0 0", fontSize: "0.8rem" }}>
                  {t("form.ceilingHint", { ceiling: formatEur(ceiling) })}
                </p>
              )}
            </fieldset>

            <label>
              <span className="meta">{t("form.purposeLabel")}</span>
              <input
                type="text"
                value={purpose}
                onChange={(e) => setPurpose(e.target.value)}
                placeholder={t("form.purposePlaceholder")}
                style={{ width: "100%" }}
                maxLength={255}
              />
            </label>

            {err && <p className="error">{err}</p>}

            <div style={{ display: "flex", gap: "0.5rem" }}>
              <button type="submit" className="button" disabled={submitting || overCeiling}>
                {submitting ? t("form.submitting") : t("form.submit")}
              </button>
              <button type="button" className="ghost" onClick={() => setOpen(false)}>
                {t("form.cancel")}
              </button>
            </div>
          </div>
        </form>
      )}
    </section>
  );
}

interface SponsorshipListProps {
  rows: SponsorshipOut[];
  mode: "emitted" | "received";
  defaults: SponsorshipDefaultsOut | null;
  onChange: () => Promise<void>;
}

function SponsorshipList({ rows, mode, defaults, onChange }: SponsorshipListProps) {
  const t = useTranslations("sponsorships");
  if (rows.length === 0) {
    return <p className="meta">{t(mode === "emitted" ? "empty.emitted" : "empty.received")}</p>;
  }
  return (
    <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: "0.75rem" }}>
      {rows.map((row) => (
        <li key={row.id}>
          <SponsorshipRow row={row} mode={mode} defaults={defaults} onChange={onChange} />
        </li>
      ))}
    </ul>
  );
}

interface SponsorshipRowProps {
  row: SponsorshipOut;
  mode: "emitted" | "received";
  defaults: SponsorshipDefaultsOut | null;
  onChange: () => Promise<void>;
}

function SponsorshipRow({ row, mode, defaults, onChange }: SponsorshipRowProps) {
  const t = useTranslations("sponsorships");
  const [editingCap, setEditingCap] = useState(false);
  const [newCapEur, setNewCapEur] = useState((row.cap_cents / 100).toFixed(2));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const remaining = row.remaining_cents;
  const pct = useMemo(() => {
    if (row.cap_cents <= 0) return 0;
    return Math.min(100, Math.round((row.spent_cents / row.cap_cents) * 100));
  }, [row.cap_cents, row.spent_cents]);

  const tint = pct >= 95 ? "#d33" : pct >= 80 ? "#c80" : "var(--bv-fg, #444)";

  async function save() {
    setErr(null);
    const eur = Number.parseFloat(newCapEur);
    if (Number.isNaN(eur) || eur <= 0) {
      setErr(t("row.errors.capPositive"));
      return;
    }
    const newCents = Math.round(eur * 100);
    if (defaults?.ceiling_cents && newCents > defaults.ceiling_cents) {
      setErr(t("form.errors.capOverCeiling", { ceiling: formatEur(defaults.ceiling_cents) }));
      return;
    }
    setBusy(true);
    try {
      await sponsorshipsApi.updateCap(row.id, newCents);
      setEditingCap(false);
      await onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "update failed");
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (!confirm(t("row.confirmRevoke"))) return;
    setBusy(true);
    setErr(null);
    try {
      await sponsorshipsApi.revoke(row.id);
      await onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "revoke failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card" style={{ padding: "0.75rem" }}>
      <header
        style={{ display: "flex", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap" }}
      >
        <div>
          <strong>
            {t(`scopeKinds.${row.scope_kind}` as never)}
            {row.scope_id && (
              <span className="meta" style={{ fontWeight: 400 }}>
                {" "}
                · {row.scope_id.slice(0, 8)}…
              </span>
            )}
          </strong>
          <p className="meta" style={{ margin: "0.2rem 0 0", fontSize: "0.85rem" }}>
            {mode === "emitted"
              ? t("row.sponsoredLabel", { id: row.sponsored_subject_id.slice(0, 8) })
              : t("row.sponsorLabel", { id: row.sponsor_subject_id.slice(0, 8) })}
            {row.purpose && ` · ${row.purpose}`}
          </p>
        </div>
        <div style={{ textAlign: "right", color: tint }}>
          <strong>{formatEur(row.spent_cents)}</strong>
          <span className="meta"> / {formatEur(row.cap_cents)}</span>
        </div>
      </header>

      <progress
        value={row.spent_cents}
        max={row.cap_cents}
        style={{ width: "100%", height: "0.4rem", marginTop: "0.4rem" }}
      />

      <p className="meta" style={{ margin: "0.4rem 0 0", fontSize: "0.85rem" }}>
        {t("row.remaining", { remaining: formatEur(remaining) })}
        {row.valid_until && ` · ${t("row.validUntil")}: ${formatDateTime(row.valid_until)}`}
      </p>

      {err && <p className="error">{err}</p>}

      {mode === "emitted" && (
        <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem", flexWrap: "wrap" }}>
          {!editingCap && (
            <button
              type="button"
              className="ghost"
              onClick={() => setEditingCap(true)}
              disabled={busy}
            >
              {t("row.editCap")}
            </button>
          )}
          {editingCap && (
            <>
              <input
                type="number"
                min={0.01}
                step={0.01}
                value={newCapEur}
                onChange={(e) => setNewCapEur(e.target.value)}
                style={{ width: "8rem" }}
                aria-label={t("row.newCapAria")}
              />
              <span className="meta">€</span>
              <button type="button" className="button" onClick={save} disabled={busy}>
                {busy ? t("form.submitting") : t("row.saveCap")}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => {
                  setEditingCap(false);
                  setNewCapEur((row.cap_cents / 100).toFixed(2));
                }}
                disabled={busy}
              >
                {t("form.cancel")}
              </button>
            </>
          )}
          <button type="button" className="ghost" onClick={revoke} disabled={busy}>
            {t("row.revoke")}
          </button>
        </div>
      )}
    </article>
  );
}
