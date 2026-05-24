"use client";

// /settings/calendar — public iCal subscription handles + external
// provider integration. Each patient row mints / shows / revokes a
// public, HMAC-signed, non-expiring feed URL (backend:
// /api/calendar/feed/{token}.ics). Google Calendar OAuth comes later
// (button disabled with a "coming soon" tooltip).

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  type CalendarSubscription,
  type Patient,
  calendarSubscriptionsApi,
  patientsApi,
} from "@/lib/api";

const ROW_BORDER = "1px solid var(--bv-card-border, #e5e7eb)";

function errText(e: unknown): string {
  return e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : String(e);
}

function PatientSubscriptionRow({ patient }: { patient: Patient }): React.JSX.Element {
  const t = useTranslations("settingsCalendar");
  const [sub, setSub] = useState<CalendarSubscription | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await calendarSubscriptionsApi.list(patient.id);
      setSub(list.find((s) => s.revoked_at === null) ?? null);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoaded(true);
    }
  }, [patient.id]);

  useEffect(() => {
    let cancelled = false;
    calendarSubscriptionsApi
      .list(patient.id)
      .then((list) => {
        if (!cancelled) setSub(list.find((s) => s.revoked_at === null) ?? null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errText(e));
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [patient.id]);

  async function generate(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const created = await calendarSubscriptionsApi.create(patient.id);
      setSub(created);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(): Promise<void> {
    if (sub === null) return;
    if (typeof window !== "undefined" && !window.confirm(t("revokeConfirm"))) return;
    setBusy(true);
    setError(null);
    try {
      await calendarSubscriptionsApi.revoke(patient.id, sub.id);
      setSub(null);
      await refresh();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  function copy(): void {
    if (sub === null || typeof window === "undefined") return;
    navigator.clipboard?.writeText(sub.feed_url).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    });
  }

  const name = patient.display_name ?? patient.id;
  const fmt = (iso: string) => new Date(iso).toLocaleString();

  return (
    <li
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "0.5rem",
        padding: "0.55rem 0",
        borderBottom: ROW_BORDER,
      }}
    >
      <strong style={{ flex: "0 0 200px" }}>{name}</strong>

      {!loaded ? (
        <span className="meta" style={{ flex: 1 }} aria-live="polite">
          …
        </span>
      ) : sub ? (
        <>
          <input
            type="text"
            readOnly
            value={sub.feed_url}
            aria-label={`${name} — iCal URL`}
            onFocus={(e) => e.currentTarget.select()}
            style={{
              flex: 1,
              minWidth: "12rem",
              fontSize: "0.78rem",
              fontFamily: "var(--bv-mono, ui-monospace, monospace)",
              color: "var(--bv-fg-soft)",
              padding: "0.2rem 0.4rem",
              border: ROW_BORDER,
              borderRadius: 4,
              background: "var(--bv-input-bg, transparent)",
            }}
          />
          <button
            type="button"
            onClick={copy}
            aria-label={`${t("copy")} — ${name}`}
            style={{ fontSize: "0.78rem", padding: "0.2rem 0.6rem" }}
          >
            {copied ? `✓ ${t("copied")}` : t("copy")}
          </button>
          <button
            type="button"
            onClick={revoke}
            disabled={busy}
            aria-label={`${t("revoke")} — ${name}`}
            style={{
              fontSize: "0.78rem",
              padding: "0.2rem 0.6rem",
              color: "var(--bv-danger, #c00)",
            }}
          >
            {busy ? t("revoking") : t("revoke")}
          </button>
          <span className="meta" style={{ flexBasis: "100%", fontSize: "0.72rem" }}>
            {t("activeSince", { date: fmt(sub.created_at) })}
            {" · "}
            {sub.last_accessed_at
              ? t("lastAccessed", {
                  date: fmt(sub.last_accessed_at),
                  count: sub.access_count,
                })
              : t("neverAccessed")}
          </span>
        </>
      ) : (
        <>
          <span className="meta" style={{ flex: 1, fontSize: "0.8rem" }}>
            {t("noLinkYet")}
          </span>
          <button
            type="button"
            onClick={generate}
            disabled={busy}
            style={{ fontSize: "0.8rem", padding: "0.3rem 0.7rem" }}
          >
            {busy ? t("generating") : t("generate")}
          </button>
        </>
      )}

      {error && (
        <span
          role="alert"
          className="meta"
          style={{ flexBasis: "100%", color: "var(--bv-danger, #c00)", fontSize: "0.72rem" }}
        >
          {t("actionError", { detail: error })}
        </span>
      )}
    </li>
  );
}

export default function SettingsCalendarPage(): React.JSX.Element {
  const t = useTranslations("settingsCalendar");
  const [patients, setPatients] = useState<Patient[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    patientsApi
      .list({ limit: 200 })
      .then((page) => {
        if (!cancelled) setPatients(page.items ?? []);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errText(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main style={{ padding: "1rem" }}>
      <p className="meta">
        <Link href="/settings">← {t("backToSettings")}</Link>
      </p>
      <h1>{t("title")}</h1>

      <section style={{ marginTop: "1rem" }}>
        <h2 style={{ fontSize: "1rem" }}>{t("subscriptionsHeading")}</h2>
        <p className="meta">{t("subscriptionsIntro")}</p>
        {error && (
          <p role="alert" style={{ color: "var(--bv-danger, #c00)" }}>
            {t("loadError", { detail: error })}
          </p>
        )}
        <ul style={{ listStyle: "none", padding: 0 }}>
          {patients.map((p) => (
            <PatientSubscriptionRow key={p.id} patient={p} />
          ))}
        </ul>
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h2 style={{ fontSize: "1rem" }}>{t("externalHeading")}</h2>
        <p className="meta">{t("externalIntro")}</p>
        <button
          type="button"
          disabled
          aria-disabled
          title={t("comingSoonTitle")}
          style={{ fontSize: "0.85rem", padding: "0.4rem 0.8rem" }}
        >
          {t("connectGoogle")}
        </button>
      </section>
    </main>
  );
}
