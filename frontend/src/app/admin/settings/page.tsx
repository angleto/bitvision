"use client";

// Admin-tunable runtime configuration. Edits are key-by-key, written
// to the ``app_settings`` table via PATCH /api/admin/settings/{key}.
// The ``value`` column is JSONB so each setting decides its own type;
// the editor parses the input as JSON and falls back to a literal
// string when JSON parsing fails (so simple text values work without
// the user having to wrap them in quotes).
//
// New settings show up automatically — anything in the table is
// listed. Adding a brand-new key from this UI is supported via the
// "Add setting" form at the bottom.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useModal } from "@/components/ModalHost";
import { ApiError, type AppSetting, settingsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

function parseValue(input: string): unknown {
  // Try strict JSON first ('123', '"abc"', 'true', '{...}'). If that
  // fails, treat the whole thing as a plain string. Distinguishing
  // makes ``42`` (number) different from ``"42"`` (string).
  const trimmed = input.trim();
  if (trimmed === "") return "";
  try {
    return JSON.parse(trimmed);
  } catch {
    return input;
  }
}

function stringifyValue(v: unknown): string {
  if (typeof v === "string") return JSON.stringify(v);
  return JSON.stringify(v, null, 2);
}

export default function AdminSettingsPage() {
  const { user, status } = useAuth();
  const t = useTranslations("adminSettings");
  const modal = useModal();
  const [rows, setRows] = useState<AppSetting[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [newScope, setNewScope] = useState<"public" | "admin">("admin");
  const [newDescription, setNewDescription] = useState("");

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const data = await settingsApi.listAll();
      setRows(data);
      const next: Record<string, string> = {};
      for (const r of data) next[r.key] = stringifyValue(r.value);
      setDrafts(next);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    if (status === "ready" && user?.is_admin) refresh();
  }, [status, user?.is_admin, refresh]);

  if (status !== "ready") {
    return (
      <main style={{ padding: "1.25rem" }}>
        <p className="meta">…</p>
      </main>
    );
  }
  if (!user || !user.is_admin) {
    return (
      <main style={{ padding: "1.25rem" }}>
        <p className="error">{t("forbidden")}</p>
      </main>
    );
  }

  async function saveOne(key: string) {
    setSaving(key);
    setErr(null);
    try {
      const draft = drafts[key] ?? "";
      const value = parseValue(draft);
      await settingsApi.upsert(key, { value });
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("saveFailed"));
    } finally {
      setSaving(null);
    }
  }

  async function createNew(e: React.FormEvent) {
    e.preventDefault();
    if (!newKey.trim()) return;
    setSaving(newKey);
    setErr(null);
    try {
      await settingsApi.upsert(newKey.trim(), {
        value: parseValue(newValue),
        scope: newScope,
        description: newDescription.trim() || null,
      });
      setNewKey("");
      setNewValue("");
      setNewDescription("");
      setNewScope("admin");
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("saveFailed"));
    } finally {
      setSaving(null);
    }
  }

  return (
    <main style={{ padding: "1.25rem", maxWidth: 900, margin: "0 auto" }}>
      <h1>{t("title")}</h1>
      <p className="meta" style={{ marginBottom: "0.5rem" }}>
        {t("subtitle")}
      </p>
      <div
        className="card"
        style={{
          padding: "0.6rem 0.8rem",
          marginBottom: "1rem",
          fontSize: "0.85rem",
          background: "var(--bv-card-bg-soft, transparent)",
        }}
      >
        <strong>{t("scopeLegendTitle")}</strong>
        <ul style={{ margin: "0.3rem 0 0 1rem", padding: 0 }}>
          <li>
            <span
              style={{
                fontWeight: 600,
                color: "var(--bv-success, #2c8a4d)",
              }}
            >
              public
            </span>{" "}
            — {t("scopePublicHint")}
          </li>
          <li>
            <span
              style={{
                fontWeight: 600,
                color: "var(--bv-error, #cf6e6e)",
              }}
            >
              admin
            </span>{" "}
            — {t("scopeAdminHint")}
          </li>
        </ul>
      </div>
      {err && <p className="error">{err}</p>}

      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.88rem",
        }}
      >
        <thead>
          <tr style={{ textAlign: "left", borderBottom: "1px solid var(--bv-card-border)" }}>
            <th style={cellHead}>{t("key")}</th>
            <th style={cellHead}>{t("scope")}</th>
            <th style={cellHead}>{t("value")}</th>
            <th style={cellHead}>{t("actions")}</th>
          </tr>
        </thead>
        <tbody>
          {rows?.map((r) => (
            <tr key={r.key} style={{ borderBottom: "1px solid var(--bv-card-border)" }}>
              <td style={cellBody}>
                <code style={{ fontSize: "0.78rem" }}>{r.key}</code>
                {r.description && (
                  <div className="meta" style={{ fontSize: "0.7rem", marginTop: 2 }}>
                    {r.description}
                  </div>
                )}
              </td>
              <td style={cellBody}>
                <span
                  className="badge"
                  style={{
                    display: "inline-block",
                    fontSize: "0.72rem",
                    fontWeight: 600,
                    padding: "0.15rem 0.5rem",
                    borderRadius: 999,
                    border: "1px solid",
                    background:
                      r.scope === "public"
                        ? "rgba(44, 138, 77, 0.15)"
                        : "rgba(207, 110, 110, 0.15)",
                    borderColor:
                      r.scope === "public"
                        ? "var(--bv-success, #2c8a4d)"
                        : "var(--bv-error, #cf6e6e)",
                    color:
                      r.scope === "public"
                        ? "var(--bv-success, #2c8a4d)"
                        : "var(--bv-error, #cf6e6e)",
                  }}
                  title={r.scope === "public" ? t("scopePublicHint") : t("scopeAdminHint")}
                >
                  {r.scope}
                </span>
              </td>
              <td style={cellBody}>
                <textarea
                  value={drafts[r.key] ?? ""}
                  onChange={(e) => setDrafts((prev) => ({ ...prev, [r.key]: e.target.value }))}
                  rows={Math.min(4, Math.max(1, (drafts[r.key] ?? "").split("\n").length))}
                  style={{
                    width: "100%",
                    fontFamily: "monospace",
                    fontSize: "0.78rem",
                  }}
                />
              </td>
              <td style={cellBody}>
                <button
                  type="button"
                  disabled={saving === r.key}
                  onClick={() => saveOne(r.key)}
                  style={{ fontSize: "0.78rem" }}
                >
                  {saving === r.key ? "…" : t("save")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2 style={{ marginTop: "1.5rem" }}>{t("addNew")}</h2>
      <form
        onSubmit={createNew}
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "0.6rem",
          maxWidth: 720,
        }}
      >
        <label>
          <span className="meta" style={{ fontSize: "0.75rem" }}>
            {t("key")}
          </span>
          <input
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder="e.g. viewer.marker.fade.range"
            required
            style={{ width: "100%", fontFamily: "monospace" }}
          />
        </label>
        <label>
          <span className="meta" style={{ fontSize: "0.75rem" }}>
            {t("scope")}
          </span>
          <select
            value={newScope}
            onChange={(e) => setNewScope(e.target.value as "public" | "admin")}
            style={{ width: "100%" }}
          >
            <option value="admin">admin</option>
            <option value="public">public</option>
          </select>
        </label>
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta" style={{ fontSize: "0.75rem" }}>
            {t("value")}
          </span>
          <textarea
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            placeholder='2 / "string" / true / {"a": 1}'
            rows={3}
            style={{ width: "100%", fontFamily: "monospace" }}
          />
        </label>
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta" style={{ fontSize: "0.75rem" }}>
            {t("description")}
          </span>
          <input
            value={newDescription}
            onChange={(e) => setNewDescription(e.target.value)}
            style={{ width: "100%" }}
          />
        </label>
        <div style={{ gridColumn: "1 / -1", textAlign: "right" }}>
          <button type="submit" disabled={!newKey.trim() || saving !== null}>
            {saving === newKey ? "…" : t("create")}
          </button>
        </div>
      </form>
    </main>
  );
}

const cellHead: React.CSSProperties = {
  padding: "0.4rem 0.5rem",
  fontWeight: 600,
  fontSize: "0.78rem",
  letterSpacing: "0.04em",
  textTransform: "uppercase",
};

const cellBody: React.CSSProperties = {
  padding: "0.5rem 0.5rem",
  verticalAlign: "top",
};
