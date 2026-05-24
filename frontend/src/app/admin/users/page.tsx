"use client";

// Admin user dashboard. Lists every user with current storage usage,
// active jobs, lock status. Each row exposes:
//   - "Edit" → open the per-user form (storage quota, job cap,
//     blocked, blocked reason, admin role)
//   - "Block" / "Unblock" → toggle is_active in one click
//   - "Delete" → hard-delete the user row (CASCADE) with a confirm
//
// Visible only to admins; the route guard below short-circuits to a
// 403 message if the caller isn't admin (the endpoints would also
// 403 server-side, but we save the round trip).

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useModal } from "@/components/ModalHost";
import NativeDialog from "@/components/NativeDialog";
import {
  type AdminUser,
  ApiError,
  type PlatformDefaults,
  adminUsersApi,
  creditsApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const GIB = 1024 ** 3;

function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n === 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
  return `${(n / 1024 ** 3).toFixed(2)} GiB`;
}

export default function AdminUsersPage() {
  const t = useTranslations("adminUsers");
  const { user, status } = useAuth();
  const modal = useModal();
  const [rows, setRows] = useState<AdminUser[] | null>(null);
  const [total, setTotal] = useState(0);
  const [defaults, setDefaults] = useState<PlatformDefaults | null>(null);
  const [q, setQ] = useState("");
  const [blockedFilter, setBlockedFilter] = useState<"all" | "active" | "blocked">("all");
  const [editing, setEditing] = useState<AdminUser | null>(null);
  const [topupTarget, setTopupTarget] = useState<AdminUser | null>(null);
  const [quotaTarget, setQuotaTarget] = useState<AdminUser | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const params: Parameters<typeof adminUsersApi.list>[0] = { limit: 200 };
      if (q.trim()) params.q = q.trim();
      if (blockedFilter === "blocked") params.blocked = true;
      if (blockedFilter === "active") params.blocked = false;
      const page = await adminUsersApi.list(params);
      setRows(page.items);
      setTotal(page.total);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [q, blockedFilter, t]);

  // Wait until ``useAuth`` has populated the JWT (its initial render
  // returns ``status="loading"`` with no token in localStorage yet).
  // Firing the fetch before then sends an unauthenticated request and
  // the backend correctly returns 401 — even for admins.
  useEffect(() => {
    if (status !== "ready" || !user || !user.is_admin) return;
    refresh();
    adminUsersApi
      .platformDefaults()
      .then(setDefaults)
      .catch(() => {});
  }, [refresh, status, user]);

  const onToggleBlocked = useCallback(
    async (u: AdminUser) => {
      setBusy(u.subject_id);
      try {
        await adminUsersApi.update(u.subject_id, {
          is_active: !u.is_active,
          blocked_reason: u.is_active ? t("defaultBlockReason") : null,
        });
        await refresh();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("loadFailed"));
      } finally {
        setBusy(null);
      }
    },
    [refresh, t],
  );

  const onDelete = useCallback(
    async (u: AdminUser) => {
      const ok = await modal.confirm({
        message: t("deleteConfirm", { email: u.email }),
        destructive: true,
      });
      if (!ok) return;
      setBusy(u.subject_id);
      try {
        await adminUsersApi.remove(u.subject_id);
        await refresh();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("loadFailed"));
      } finally {
        setBusy(null);
      }
    },
    [refresh, t, modal],
  );

  if (status === "loading") return <p style={{ padding: "1rem" }}>…</p>;
  if (!user || !user.is_admin) {
    return (
      <main style={{ padding: "1.25rem" }}>
        <h1>{t("title")}</h1>
        <p style={{ color: "var(--bv-error, #cf6e6e)" }}>{t("forbidden")}</p>
      </main>
    );
  }

  return (
    <main style={{ padding: "1.25rem", maxWidth: 1200 }}>
      <header
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: "1rem",
          flexWrap: "wrap",
          marginBottom: "0.75rem",
        }}
      >
        <h1 style={{ margin: 0 }}>{t("title")}</h1>
        {defaults && (
          <span className="meta" style={{ fontSize: "0.85rem" }}>
            {t("platformDefault", {
              quota: formatBytes(defaults.storage_free_tier_bytes),
            })}
          </span>
        )}
      </header>
      <p className="meta" style={{ fontSize: "0.85rem", marginBottom: "0.75rem" }}>
        {t("intro")}
      </p>

      <div
        style={{
          display: "flex",
          gap: "0.6rem",
          marginBottom: "0.75rem",
          flexWrap: "wrap",
        }}
      >
        <input
          type="search"
          value={q}
          placeholder={t("searchPlaceholder")}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") refresh();
          }}
          style={{ minWidth: 280, padding: "0.4rem 0.6rem" }}
        />
        <select
          value={blockedFilter}
          onChange={(e) => setBlockedFilter(e.target.value as typeof blockedFilter)}
          style={{ padding: "0.4rem 0.6rem" }}
        >
          <option value="all">{t("filterAll")}</option>
          <option value="active">{t("filterActive")}</option>
          <option value="blocked">{t("filterBlocked")}</option>
        </select>
        <button type="button" onClick={refresh}>
          {t("refresh")}
        </button>
        {rows && (
          <span className="meta" style={{ alignSelf: "center", fontSize: "0.82rem" }}>
            {t("countShown", { shown: rows.length, total })}
          </span>
        )}
      </div>

      {err && <p style={{ color: "var(--bv-error, #cf6e6e)", margin: "0.5rem 0" }}>{err}</p>}

      {rows === null ? (
        <p>…</p>
      ) : rows.length === 0 ? (
        <p className="meta">{t("empty")}</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.88rem" }}>
          <thead>
            <tr style={{ textAlign: "left", borderBottom: "1px solid var(--bv-card-border)" }}>
              <th style={{ padding: "0.4rem 0.5rem" }}>{t("colEmail")}</th>
              <th style={{ padding: "0.4rem 0.5rem" }}>{t("colRole")}</th>
              <th style={{ padding: "0.4rem 0.5rem" }}>{t("colStatus")}</th>
              <th style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{t("colStorage")}</th>
              <th style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{t("colWallet")}</th>
              <th style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{t("colJobs")}</th>
              <th style={{ padding: "0.4rem 0.5rem" }}>{t("colJoined")}</th>
              <th style={{ padding: "0.4rem 0.5rem" }}>{t("colActions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((u) => (
              <UserRow
                key={u.subject_id}
                u={u}
                self={u.subject_id === user.subject_id}
                busy={busy === u.subject_id}
                onEdit={() => setEditing(u)}
                onTopup={() => setTopupTarget(u)}
                onQuota={() => setQuotaTarget(u)}
                onToggleBlocked={() => onToggleBlocked(u)}
                onDelete={() => onDelete(u)}
              />
            ))}
          </tbody>
        </table>
      )}

      {editing && (
        <EditUserDialog
          user={editing}
          isSelf={editing.subject_id === user.subject_id}
          defaults={defaults}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}

      {topupTarget && (
        <TopupDialog
          user={topupTarget}
          onClose={() => setTopupTarget(null)}
          onSaved={() => {
            setTopupTarget(null);
            refresh();
          }}
        />
      )}

      {quotaTarget && (
        <QuotaDialog
          user={quotaTarget}
          defaults={defaults}
          onClose={() => setQuotaTarget(null)}
          onSaved={() => {
            setQuotaTarget(null);
            refresh();
          }}
        />
      )}
    </main>
  );
}

function UserRow({
  u,
  self,
  busy,
  onEdit,
  onTopup,
  onQuota,
  onToggleBlocked,
  onDelete,
}: {
  u: AdminUser;
  self: boolean;
  busy: boolean;
  onEdit: () => void;
  onTopup: () => void;
  onQuota: () => void;
  onToggleBlocked: () => void;
  onDelete: () => void;
}) {
  const t = useTranslations("adminUsers");
  const usagePct = Math.min(
    100,
    Math.round((u.storage_used_bytes / Math.max(1, u.effective_storage_quota_bytes)) * 100),
  );
  const statusLabel = !u.is_active ? t("statusBlocked") : t("statusActive");
  return (
    <tr style={{ borderBottom: "1px solid var(--bv-card-border)" }}>
      <td style={{ padding: "0.4rem 0.5rem" }}>
        <strong>{u.email}</strong>
        {self && (
          <span
            style={{
              marginLeft: "0.4rem",
              fontSize: "0.72rem",
              padding: "1px 6px",
              borderRadius: 4,
              background: "var(--bv-accent, #e96b1f)",
              color: "#fff",
            }}
          >
            {t("selfBadge")}
          </span>
        )}
      </td>
      <td style={{ padding: "0.4rem 0.5rem" }}>
        {u.is_admin ? <strong>admin</strong> : <span className="meta">user</span>}
      </td>
      <td style={{ padding: "0.4rem 0.5rem" }}>
        <span
          style={{
            color: u.is_active ? "var(--bv-success, #2c8a4d)" : "var(--bv-error, #cf6e6e)",
          }}
        >
          {statusLabel}
        </span>
        {u.blocked_reason && (
          <div className="meta" style={{ fontSize: "0.72rem" }}>
            {u.blocked_reason}
          </div>
        )}
      </td>
      <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>
        <div>
          {formatBytes(u.storage_used_bytes)} / {formatBytes(u.effective_storage_quota_bytes)}
        </div>
        <div className="meta" style={{ fontSize: "0.72rem" }}>
          {usagePct}%
          {u.storage_quota_bytes !== null && (
            <span style={{ marginLeft: "0.3rem" }}>· {t("override")}</span>
          )}
        </div>
      </td>
      <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>
        <div>{(u.wallet_balance_cents / 100).toFixed(2)} €</div>
        <div className="meta" style={{ fontSize: "0.72rem" }}>
          {u.wallet_balance_cents.toLocaleString()} cents
        </div>
      </td>
      <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>
        <div>
          {u.active_job_count}
          {u.max_concurrent_jobs !== null && (
            <span className="meta"> / {u.max_concurrent_jobs}</span>
          )}
        </div>
      </td>
      <td style={{ padding: "0.4rem 0.5rem" }}>{u.created_at.slice(0, 10)}</td>
      <td style={{ padding: "0.4rem 0.5rem" }}>
        <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
          <button type="button" className="ghost" onClick={onEdit} disabled={busy}>
            {t("edit")}
          </button>
          <button type="button" className="ghost" onClick={onTopup} disabled={busy}>
            {t("topup")}
          </button>
          <button type="button" className="ghost" onClick={onQuota} disabled={busy}>
            {t("quota")}
          </button>
          {!self && (
            <>
              <button type="button" className="ghost" onClick={onToggleBlocked} disabled={busy}>
                {u.is_active ? t("block") : t("unblock")}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={onDelete}
                disabled={busy}
                style={{ color: "var(--bv-error, #cf6e6e)" }}
              >
                {t("delete")}
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

function EditUserDialog({
  user,
  isSelf,
  defaults,
  onClose,
  onSaved,
}: {
  user: AdminUser;
  isSelf: boolean;
  defaults: PlatformDefaults | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useTranslations("adminUsers");
  const [storageGiB, setStorageGiB] = useState<string>(
    user.storage_quota_bytes !== null ? (user.storage_quota_bytes / GIB).toFixed(2) : "",
  );
  const [maxJobs, setMaxJobs] = useState<string>(
    user.max_concurrent_jobs !== null ? String(user.max_concurrent_jobs) : "",
  );
  const [isAdmin, setIsAdmin] = useState(user.is_admin);
  const [isActive, setIsActive] = useState(user.is_active);
  const [blockedReason, setBlockedReason] = useState(user.blocked_reason ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const placeholderQuotaGiB = useMemo(
    () => (defaults ? (defaults.storage_free_tier_bytes / GIB).toFixed(0) : "10"),
    [defaults],
  );

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      const body: Parameters<typeof adminUsersApi.update>[1] = {};
      // Storage quota: empty string → clear override; numeric → set in
      // bytes (multiply GiB by 2^30).
      if (storageGiB.trim() === "") {
        body.clear_storage_quota = true;
      } else {
        const v = Number.parseFloat(storageGiB);
        if (!Number.isFinite(v) || v < 0) {
          throw new Error(t("invalidQuota"));
        }
        body.storage_quota_bytes = Math.round(v * GIB);
      }
      if (maxJobs.trim() === "") {
        body.clear_max_concurrent_jobs = true;
      } else {
        const v = Number.parseInt(maxJobs, 10);
        if (!Number.isFinite(v) || v < 1) {
          throw new Error(t("invalidMaxJobs"));
        }
        body.max_concurrent_jobs = v;
      }
      body.is_admin = isAdmin;
      body.is_active = isActive;
      body.blocked_reason = isActive ? null : blockedReason || null;
      await adminUsersApi.update(user.subject_id, body);
      onSaved();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <NativeDialog open onClose={onClose} className="bv-dialog">
      <form
        onSubmit={submit}
        style={{
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-md)",
          padding: "1.25rem 1.4rem",
          maxWidth: 520,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1rem" }}>{t("editTitle", { email: user.email })}</h3>

        <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem" }}>
          <span style={{ fontSize: "0.78rem", color: "var(--bv-fg-soft)" }}>
            {t("fieldStorageQuota", { def: placeholderQuotaGiB })}
          </span>
          <input
            type="number"
            inputMode="decimal"
            min={0}
            step="0.5"
            value={storageGiB}
            placeholder={placeholderQuotaGiB}
            onChange={(e) => setStorageGiB(e.target.value)}
            disabled={submitting}
          />
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem" }}>
          <span style={{ fontSize: "0.78rem", color: "var(--bv-fg-soft)" }}>
            {t("fieldMaxJobs")}
          </span>
          <input
            type="number"
            min={1}
            step={1}
            value={maxJobs}
            placeholder={t("inheritDefault")}
            onChange={(e) => setMaxJobs(e.target.value)}
            disabled={submitting}
          />
        </label>

        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
          <input
            type="checkbox"
            checked={isAdmin}
            onChange={(e) => setIsAdmin(e.target.checked)}
            disabled={submitting || isSelf}
          />
          <span>{t("fieldIsAdmin")}</span>
          {isSelf && (
            <span className="meta" style={{ fontSize: "0.72rem" }}>
              ({t("selfAdminLock")})
            </span>
          )}
        </label>

        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
          <input
            type="checkbox"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
            disabled={submitting}
          />
          <span>{t("fieldIsActive")}</span>
        </label>

        {!isActive && (
          <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem" }}>
            <span style={{ fontSize: "0.78rem", color: "var(--bv-fg-soft)" }}>
              {t("fieldBlockedReason")}
            </span>
            <input
              type="text"
              maxLength={255}
              value={blockedReason}
              placeholder={t("defaultBlockReason")}
              onChange={(e) => setBlockedReason(e.target.value)}
              disabled={submitting}
            />
          </label>
        )}

        {err && (
          <p style={{ color: "var(--bv-error, #cf6e6e)", fontSize: "0.82rem", margin: 0 }}>{err}</p>
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "0.4rem",
          }}
        >
          <button type="button" className="ghost" onClick={onClose} disabled={submitting}>
            {t("cancel")}
          </button>
          <button type="submit" disabled={submitting}>
            {submitting ? t("saving") : t("save")}
          </button>
        </div>
      </form>
    </NativeDialog>
  );
}

function TopupDialog({
  user,
  onClose,
  onSaved,
}: {
  user: AdminUser;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useTranslations("adminUsers");
  const [amountEur, setAmountEur] = useState<string>("10.00");
  const [reason, setReason] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      const eur = Number.parseFloat(amountEur);
      if (!Number.isFinite(eur) || eur <= 0) {
        throw new Error(t("topupInvalidAmount"));
      }
      const cents = Math.round(eur * 100);
      const idempotency_key = `admin-topup-${user.subject_id}-${Date.now()}`;
      const out = await creditsApi.adminTopup({
        user_subject_id: user.subject_id,
        amount_cents: cents,
        idempotency_key,
        reason: reason.trim() || undefined,
      });
      // Brief success feedback in the dialog before refresh would race
      // with the parent's refetch; just close and let the table update.
      void out;
      onSaved();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <NativeDialog open onClose={onClose} className="bv-dialog">
      <form
        onSubmit={submit}
        style={{
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-md)",
          padding: "1.25rem 1.4rem",
          maxWidth: 460,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
        }}
      >
        <h3 style={{ margin: 0 }}>{t("topupTitle")}</h3>
        <p className="meta" style={{ margin: 0 }}>
          {t("topupSubtitle", { email: user.email })}
        </p>
        <div className="meta" style={{ fontSize: "0.85rem" }}>
          {t("topupCurrentBalance")}:{" "}
          <strong>
            {(user.wallet_balance_cents / 100).toFixed(2)} € (
            {user.wallet_balance_cents.toLocaleString()} cents)
          </strong>
        </div>

        <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span className="meta">{t("topupAmountLabel")}</span>
          <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
            <input
              type="number"
              min="0.01"
              step="0.01"
              value={amountEur}
              onChange={(e) => setAmountEur(e.target.value)}
              required
              style={{ flex: 1, padding: "0.4rem 0.6rem" }}
            />
            <span>€</span>
          </div>
          <span className="meta" style={{ fontSize: "0.78rem" }}>
            {t("topupAmountHint")}
          </span>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span className="meta">{t("topupReasonLabel")}</span>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t("topupReasonPlaceholder")}
            maxLength={255}
            style={{ padding: "0.4rem 0.6rem" }}
          />
        </label>

        {err && <p style={{ color: "var(--bv-error, #cf6e6e)", margin: 0 }}>{err}</p>}

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "0.4rem",
          }}
        >
          <button type="button" className="ghost" onClick={onClose} disabled={submitting}>
            {t("cancel")}
          </button>
          <button type="submit" disabled={submitting}>
            {submitting ? t("saving") : t("topupSubmit")}
          </button>
        </div>
      </form>
    </NativeDialog>
  );
}

function QuotaDialog({
  user,
  defaults,
  onClose,
  onSaved,
}: {
  user: AdminUser;
  defaults: PlatformDefaults | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useTranslations("adminUsers");
  const [quotaGiB, setQuotaGiB] = useState<string>(
    user.storage_quota_bytes !== null ? (user.storage_quota_bytes / GIB).toFixed(2) : "",
  );
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const platformDefaultGiB = defaults ? (defaults.storage_free_tier_bytes / GIB).toFixed(0) : "—";
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      const body: Parameters<typeof adminUsersApi.update>[1] = {};
      if (quotaGiB.trim() === "") {
        body.clear_storage_quota = true;
      } else {
        const v = Number.parseFloat(quotaGiB);
        if (!Number.isFinite(v) || v < 0) throw new Error(t("invalidQuota"));
        body.storage_quota_bytes = Math.round(v * GIB);
      }
      await adminUsersApi.update(user.subject_id, body);
      onSaved();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setSubmitting(false);
    }
  };
  const usagePct = Math.min(
    100,
    Math.round((user.storage_used_bytes / Math.max(1, user.effective_storage_quota_bytes)) * 100),
  );
  return (
    <NativeDialog open onClose={onClose} className="bv-dialog">
      <form
        onSubmit={submit}
        style={{
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-md)",
          padding: "1.25rem 1.4rem",
          maxWidth: 480,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          gap: "0.7rem",
        }}
      >
        <h3 style={{ margin: 0 }}>{t("quotaTitle")}</h3>
        <p className="meta" style={{ margin: 0 }}>
          {t("quotaSubtitle", { email: user.email })}
        </p>
        <div
          style={{
            border: "1px solid var(--bv-card-border)",
            borderRadius: "var(--bv-r-sm, 4px)",
            padding: "0.5rem 0.75rem",
            background: "var(--bv-card-bg-soft, transparent)",
            fontSize: "0.85rem",
          }}
        >
          <div>
            <strong>{t("quotaCurrentUsage")}:</strong> {(user.storage_used_bytes / GIB).toFixed(2)}{" "}
            GiB / {(user.effective_storage_quota_bytes / GIB).toFixed(2)} GiB ({usagePct}%)
          </div>
          <div className="meta" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
            {t("quotaPlatformDefault", { default: platformDefaultGiB })}
            {user.storage_quota_bytes !== null && <span> · {t("quotaCurrentlyOverridden")}</span>}
          </div>
        </div>
        <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span className="meta">{t("quotaInputLabel")}</span>
          <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
            <input
              type="number"
              min="0"
              step="0.5"
              value={quotaGiB}
              onChange={(e) => setQuotaGiB(e.target.value)}
              placeholder={t("quotaInputPlaceholder", { default: platformDefaultGiB })}
              style={{ flex: 1, padding: "0.4rem 0.6rem" }}
            />
            <span>GiB</span>
          </div>
          <span className="meta" style={{ fontSize: "0.78rem" }}>
            {t("quotaInputHint")}
          </span>
        </label>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
          {[5, 10, 20, 50].map((preset) => (
            <button
              key={preset}
              type="button"
              className={quotaGiB === preset.toFixed(0) ? "button" : "ghost"}
              onClick={() => setQuotaGiB(preset.toFixed(0))}
            >
              {preset} GiB
            </button>
          ))}
          <button
            type="button"
            className={quotaGiB === "" ? "button" : "ghost"}
            onClick={() => setQuotaGiB("")}
            title={t("quotaClearTitle")}
          >
            {t("quotaClear")}
          </button>
        </div>
        {err && <p style={{ color: "var(--bv-error, #cf6e6e)", margin: 0 }}>{err}</p>}
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "0.4rem",
          }}
        >
          <button type="button" className="ghost" onClick={onClose} disabled={submitting}>
            {t("cancel")}
          </button>
          <button type="submit" disabled={submitting}>
            {submitting ? t("saving") : t("save")}
          </button>
        </div>
      </form>
    </NativeDialog>
  );
}
