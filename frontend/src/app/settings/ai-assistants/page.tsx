"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { useModal } from "@/components/ModalHost";
import {
  type AiAssistant,
  type AiAssistantCreateInput,
  type AiAssistantCreated,
  ApiError,
  type AssistantSharedPatient,
  type ConnectorInfo,
  type ScopeCatalogEntry,
  aiAssistantsApi,
} from "@/lib/api";

// Default-on set for the create form. Legacy read + draft-write
// scopes are sensible defaults; granular write scopes (tags, study/
// series metadata) are off by default and the user opts in. ``danger``
// scopes are NEVER on by default and require a confirmation modal
// when toggled.
const DEFAULT_ON_SCOPES = new Set<string>([
  "patient:read",
  "patient:images",
  "consultation:read",
  "consultation:write",
]);

const CATEGORY_ORDER: Array<ScopeCatalogEntry["category"]> = ["read", "write", "danger"];

export default function AiAssistantsPage() {
  const t = useTranslations("aiShare");
  const modal = useModal();
  const [assistants, setAssistants] = useState<AiAssistant[] | null>(null);
  const [connectorInfo, setConnectorInfo] = useState<ConnectorInfo | null>(null);
  const [copied, setCopied] = useState(false);
  const [revealedSecret, setRevealedSecret] = useState<AiAssistantCreated | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AiAssistant | null>(null);

  const reload = useCallback(async () => {
    try {
      setAssistants(await aiAssistantsApi.list());
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    reload();
    aiAssistantsApi
      .connectorInfo()
      .then(setConnectorInfo)
      .catch(() => {
        // Non-fatal: the page works without the URL hint.
      });
  }, [reload]);

  const copyMcpUrl = useCallback(async () => {
    if (!connectorInfo) return;
    try {
      await navigator.clipboard.writeText(connectorInfo.mcp_url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // ignore — older browsers
    }
  }, [connectorInfo]);

  const handleDelete = useCallback(
    async (a: AiAssistant) => {
      const ok = await modal.confirm({
        message: t("deleteConfirm", { label: a.label }),
        destructive: true,
        confirmLabel: t("deleteAssistant"),
      });
      if (!ok) return;
      try {
        await aiAssistantsApi.remove(a.id);
        await reload();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("actionFailed"));
      }
    },
    [modal, reload, t],
  );

  const handleSetActive = useCallback(
    async (a: AiAssistant, next: boolean) => {
      // Deactivating is the OAuth-bound equivalent of revoke: any
      // future JWT for this email is rejected at the backend gate
      // until the toggle flips back on.
      if (!next) {
        const ok = await modal.confirm({
          message: t("revokeConfirm", { label: a.label }),
          destructive: true,
          confirmLabel: t("revoke"),
        });
        if (!ok) return;
      }
      try {
        await aiAssistantsApi.setActive(a.id, next);
        await reload();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("actionFailed"));
      }
    },
    [modal, reload, t],
  );

  const handleEdit = useCallback((a: AiAssistant) => {
    setEditing(a);
    setCreating(false);
  }, []);

  const handleRotate = useCallback(
    async (a: AiAssistant) => {
      const ok = await modal.confirm({
        message: t("rotateConfirm", { label: a.label }),
        destructive: true,
        confirmLabel: t("rotateSecret"),
      });
      if (!ok) return;
      try {
        const created = await aiAssistantsApi.rotate(a.id);
        setRevealedSecret(created);
        await reload();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("actionFailed"));
      }
    },
    [modal, reload, t],
  );

  return (
    <main>
      <h1>{t("pageTitle")}</h1>
      <p className="meta" style={{ maxWidth: 720 }}>
        {t("intro")}
      </p>

      {err && <p className="error">{err}</p>}

      {connectorInfo && (
        <div
          className="card"
          style={{
            marginBottom: "1rem",
            padding: "0.75rem 1rem",
            background: "rgba(37,99,235,0.05)",
            borderColor: "var(--bv-accent, #2563eb)",
          }}
        >
          <h3 style={{ marginTop: 0, marginBottom: "0.4rem", fontSize: "1rem" }}>
            {t("connectorTitle")}
          </h3>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.4rem",
              flexWrap: "wrap",
              marginBottom: "0.4rem",
            }}
          >
            <code
              style={{
                background: "var(--bv-card-bg, #fff)",
                padding: "0.25rem 0.5rem",
                borderRadius: 4,
                fontSize: "0.95rem",
                wordBreak: "break-all",
              }}
            >
              {connectorInfo.mcp_url}
            </code>
            <button type="button" className="ghost" onClick={copyMcpUrl}>
              {copied ? t("copied") : t("copy")}
            </button>
          </div>
          <pre
            style={{
              fontFamily: "inherit",
              margin: 0,
              padding: 0,
              background: "transparent",
              fontSize: "0.85rem",
              whiteSpace: "pre-wrap",
              color: "var(--bv-meta, #4b5563)",
            }}
          >
            {connectorInfo.instructions_md}
          </pre>
        </div>
      )}

      <div style={{ margin: "1rem 0" }}>
        {!creating && (
          <button type="button" onClick={() => setCreating(true)}>
            + {t("newAssistant")}
          </button>
        )}
      </div>

      {revealedSecret && connectorInfo && (
        <CredentialsRevealCard
          assistant={revealedSecret}
          mcpUrl={connectorInfo.mcp_url}
          onClose={() => setRevealedSecret(null)}
        />
      )}

      {creating && (
        <CreateAssistantForm
          onCancel={() => setCreating(false)}
          onCreated={async (created) => {
            setCreating(false);
            setRevealedSecret(created);
            await reload();
          }}
        />
      )}

      {editing && (
        <EditAssistantForm
          assistant={editing}
          onCancel={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await reload();
          }}
        />
      )}

      {assistants === null && !err && <p className="meta">Loading…</p>}
      {assistants !== null && assistants.length === 0 && !creating && (
        <p className="meta">{t("noAssistants")}</p>
      )}

      {assistants?.map((a) => (
        <AssistantCard
          key={a.id}
          assistant={a}
          onEdit={() => handleEdit(a)}
          onRotate={() => handleRotate(a)}
          onSetActive={(next) => handleSetActive(a, next)}
          onDelete={() => handleDelete(a)}
          onPatientsChanged={reload}
        />
      ))}
    </main>
  );
}

function CreateAssistantForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (a: AiAssistantCreated) => void;
}) {
  const t = useTranslations("aiShare");
  const modal = useModal();
  const [label, setLabel] = useState("");
  const [provider, setProvider] = useState("");
  const [modelId, setModelId] = useState("");
  const [notes, setNotes] = useState("");
  const [catalog, setCatalog] = useState<ScopeCatalogEntry[] | null>(null);
  const [perms, setPerms] = useState<Record<string, boolean>>({});
  const [deidentify, setDeidentify] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    aiAssistantsApi
      .scopeCatalog()
      .then((entries) => {
        if (cancelled) return;
        setCatalog(entries);
        setPerms(Object.fromEntries(entries.map((e) => [e.key, DEFAULT_ON_SCOPES.has(e.key)])));
      })
      .catch(() => {
        if (cancelled) return;
        // Fallback: at least the four legacy scopes so the form is
        // usable even with the catalog endpoint down.
        const fallback: ScopeCatalogEntry[] = [...DEFAULT_ON_SCOPES].map((k) => ({
          key: k,
          category: "read",
          label: k,
          description: "",
          dangerous: false,
          enforced: false,
        }));
        setCatalog(fallback);
        setPerms(Object.fromEntries(fallback.map((e) => [e.key, true])));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const togglePermission = useCallback(
    async (entry: ScopeCatalogEntry, next: boolean) => {
      if (next && entry.dangerous) {
        const ok = await modal.confirm({
          message: t("dangerousScopeConfirm", { label: entry.label }),
          destructive: true,
          confirmLabel: t("dangerousScopeConfirmYes"),
        });
        if (!ok) return;
      }
      setPerms((prev) => ({ ...prev, [entry.key]: next }));
    },
    [modal, t],
  );

  const selectAllPermissions = useCallback(async () => {
    if (!catalog) return;
    const dangerCount = catalog.filter((e) => e.dangerous).length;
    if (dangerCount > 0) {
      const ok = await modal.confirm({
        message: t("selectAllConfirm", { dangerCount }),
        destructive: true,
        confirmLabel: t("selectAllConfirmYes"),
      });
      if (!ok) return;
    }
    setPerms(Object.fromEntries(catalog.map((e) => [e.key, true])));
  }, [catalog, modal, t]);

  const clearAllPermissions = useCallback(() => {
    if (!catalog) return;
    setPerms(Object.fromEntries(catalog.map((e) => [e.key, false])));
  }, [catalog]);

  const grouped = useMemo(() => {
    if (!catalog) return null;
    const out = new Map<ScopeCatalogEntry["category"], ScopeCatalogEntry[]>();
    for (const cat of CATEGORY_ORDER) out.set(cat, []);
    for (const entry of catalog) {
      out.get(entry.category)?.push(entry);
    }
    return out;
  }, [catalog]);

  const selectedCount = useMemo(() => Object.values(perms).filter(Boolean).length, [perms]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const selected = (catalog ?? []).filter((p) => perms[p.key]).map((p) => p.key);
      if (selected.length === 0) throw new Error(t("atLeastOnePerm"));
      const payload: AiAssistantCreateInput = {
        label,
        provider: provider.trim() || null,
        model_id: modelId.trim() || null,
        notes: notes.trim() || null,
        permissions: selected,
        deidentify_on_use: deidentify,
      };
      const created = await aiAssistantsApi.create(payload);
      onCreated(created);
    } catch (e) {
      setErr(
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : t("createFailed"),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={submit} style={{ marginBottom: "1rem" }}>
      <h2 style={{ marginTop: 0 }}>{t("createTitle")}</h2>
      {err && <p className="error">{err}</p>}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta">{t("labelLabel")}</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            required
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("providerLabel")}</span>
          <input
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            placeholder={t("providerPlaceholder")}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("modelIdLabel")}</span>
          <input
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
            placeholder={t("modelIdPlaceholder")}
            style={{ width: "100%" }}
          />
        </label>
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta">{t("notesLabel")}</span>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            style={{ width: "100%" }}
          />
        </label>
      </div>

      <fieldset style={{ marginTop: "0.75rem", border: "none", padding: 0 }}>
        <legend className="meta" style={{ width: "100%" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "0.5rem",
              flexWrap: "wrap",
            }}
          >
            <span>
              {t("permissions")}
              {catalog && (
                <span className="meta" style={{ marginLeft: "0.5rem", fontSize: "0.75rem" }}>
                  {t("scopesSelected", {
                    selected: selectedCount,
                    total: catalog.length,
                  })}
                </span>
              )}
            </span>
            {catalog && (
              <span style={{ display: "flex", gap: "0.4rem" }}>
                <button
                  type="button"
                  className="ghost"
                  onClick={selectAllPermissions}
                  style={{ fontSize: "0.78rem" }}
                >
                  {t("selectAllScopes")}
                </button>
                <button
                  type="button"
                  className="ghost"
                  onClick={clearAllPermissions}
                  style={{ fontSize: "0.78rem" }}
                >
                  {t("clearAllScopes")}
                </button>
              </span>
            )}
          </div>
        </legend>
        {!grouped && <p className="meta">{t("loadingScopes")}</p>}
        {grouped &&
          CATEGORY_ORDER.map((cat) => {
            const entries = grouped.get(cat) ?? [];
            if (entries.length === 0) return null;
            return (
              <div
                key={cat}
                style={{
                  marginTop: "0.5rem",
                  padding: "0.4rem 0.6rem",
                  borderLeft:
                    cat === "danger"
                      ? "3px solid #c0392b"
                      : cat === "write"
                        ? "3px solid #d68910"
                        : "3px solid #28a745",
                  background: cat === "danger" ? "rgba(192,57,43,0.05)" : "transparent",
                }}
              >
                <div
                  className="meta"
                  style={{
                    fontSize: "0.75rem",
                    textTransform: "uppercase",
                    marginBottom: "0.2rem",
                  }}
                >
                  {t(`scopeCategory_${cat}`)}
                </div>
                {entries.map((entry) => (
                  <label
                    key={entry.key}
                    style={{
                      display: "flex",
                      alignItems: "flex-start",
                      gap: "0.5rem",
                      padding: "0.25rem 0",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={!!perms[entry.key]}
                      onChange={(e) => togglePermission(entry, e.target.checked)}
                      style={{ marginTop: "0.2rem" }}
                    />
                    <div>
                      <div>
                        <strong>{entry.label}</strong>{" "}
                        <code style={{ fontSize: "0.75rem", color: "var(--bv-meta, #666)" }}>
                          {entry.key}
                        </code>
                      </div>
                      {entry.description && (
                        <div className="meta" style={{ fontSize: "0.78rem" }}>
                          {entry.description}
                        </div>
                      )}
                    </div>
                  </label>
                ))}
              </div>
            );
          })}
      </fieldset>

      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.4rem",
          marginTop: "0.75rem",
          padding: "0.5rem",
          border: "1px solid var(--bv-card-border, #e5e7eb)",
          borderRadius: 4,
        }}
      >
        <input
          type="checkbox"
          checked={deidentify}
          onChange={(e) => setDeidentify(e.target.checked)}
        />
        <div>
          <strong>{t("deidentifyLabel")}</strong>
          <div className="meta" style={{ fontSize: "0.8rem" }}>
            {t("deidentifyHint")}
          </div>
        </div>
      </label>

      <div
        style={{ marginTop: "0.75rem", display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}
      >
        <button type="button" className="ghost" onClick={onCancel} disabled={busy}>
          {t("cancel")}
        </button>
        <button type="submit" disabled={busy}>
          {busy ? t("creating") : t("create")}
        </button>
      </div>
    </form>
  );
}

function EditAssistantForm({
  assistant,
  onCancel,
  onSaved,
}: {
  assistant: AiAssistant;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const t = useTranslations("aiShare");
  const modal = useModal();
  const [label, setLabel] = useState(assistant.label);
  const [provider, setProvider] = useState(assistant.provider ?? "");
  const [modelId, setModelId] = useState(assistant.model_id ?? "");
  const [notes, setNotes] = useState(assistant.notes ?? "");
  const [catalog, setCatalog] = useState<ScopeCatalogEntry[] | null>(null);
  const [perms, setPerms] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(assistant.permissions.map((k) => [k, true])),
  );
  const [deidentify, setDeidentify] = useState(assistant.deidentify_on_use);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    aiAssistantsApi
      .scopeCatalog()
      .then((entries) => {
        if (cancelled) return;
        setCatalog(entries);
        // Preserve existing perms; default new entries (catalog adds since
        // creation) to off so the user opts in explicitly.
        setPerms((prev) => Object.fromEntries(entries.map((e) => [e.key, prev[e.key] ?? false])));
      })
      .catch(() => {
        if (cancelled) return;
        // Fallback catalog from the assistant's own permissions.
        const fallback: ScopeCatalogEntry[] = assistant.permissions.map((k) => ({
          key: k,
          category: "read",
          label: k,
          description: "",
          dangerous: false,
          enforced: false,
        }));
        setCatalog(fallback);
      });
    return () => {
      cancelled = true;
    };
  }, [assistant.permissions]);

  const togglePermission = useCallback(
    async (entry: ScopeCatalogEntry, next: boolean) => {
      if (next && entry.dangerous) {
        const ok = await modal.confirm({
          message: t("dangerousScopeConfirm", { label: entry.label }),
          destructive: true,
          confirmLabel: t("dangerousScopeConfirmYes"),
        });
        if (!ok) return;
      }
      setPerms((prev) => ({ ...prev, [entry.key]: next }));
    },
    [modal, t],
  );

  const selectAllPermissions = useCallback(async () => {
    if (!catalog) return;
    const dangerCount = catalog.filter((e) => e.dangerous).length;
    if (dangerCount > 0) {
      const ok = await modal.confirm({
        message: t("selectAllConfirm", { dangerCount }),
        destructive: true,
        confirmLabel: t("selectAllConfirmYes"),
      });
      if (!ok) return;
    }
    setPerms(Object.fromEntries(catalog.map((e) => [e.key, true])));
  }, [catalog, modal, t]);

  const clearAllPermissions = useCallback(() => {
    if (!catalog) return;
    setPerms(Object.fromEntries(catalog.map((e) => [e.key, false])));
  }, [catalog]);

  const grouped = useMemo(() => {
    if (!catalog) return null;
    const out = new Map<ScopeCatalogEntry["category"], ScopeCatalogEntry[]>();
    for (const cat of CATEGORY_ORDER) out.set(cat, []);
    for (const entry of catalog) {
      out.get(entry.category)?.push(entry);
    }
    return out;
  }, [catalog]);

  const selectedCount = useMemo(() => Object.values(perms).filter(Boolean).length, [perms]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const selected = (catalog ?? []).filter((p) => perms[p.key]).map((p) => p.key);
      if (selected.length === 0) throw new Error(t("atLeastOnePerm"));
      await aiAssistantsApi.update(assistant.id, {
        label,
        provider: provider.trim() || null,
        model_id: modelId.trim() || null,
        notes: notes.trim() || null,
        permissions: selected,
        deidentify_on_use: deidentify,
      });
      onSaved();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : e instanceof Error ? e.message : t("saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={submit} style={{ marginBottom: "1rem" }}>
      <h2 style={{ marginTop: 0 }}>{t("editTitle", { label: assistant.label })}</h2>
      {err && <p className="error">{err}</p>}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta">{t("labelLabel")}</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            required
            minLength={1}
            maxLength={255}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("providerLabel")}</span>
          <input
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            placeholder={t("providerPlaceholder")}
            maxLength={64}
            style={{ width: "100%" }}
          />
        </label>
        <label>
          <span className="meta">{t("modelIdLabel")}</span>
          <input
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
            placeholder={t("modelIdPlaceholder")}
            maxLength={128}
            style={{ width: "100%" }}
          />
        </label>
        <label style={{ gridColumn: "1 / -1" }}>
          <span className="meta">{t("notesLabel")}</span>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            style={{ width: "100%" }}
          />
        </label>
      </div>

      <fieldset style={{ marginTop: "0.75rem", border: "none", padding: 0 }}>
        <legend className="meta" style={{ width: "100%" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "0.5rem",
              flexWrap: "wrap",
            }}
          >
            <span>
              {t("permissions")}
              {catalog && (
                <span className="meta" style={{ marginLeft: "0.5rem", fontSize: "0.75rem" }}>
                  {t("scopesSelected", {
                    selected: selectedCount,
                    total: catalog.length,
                  })}
                </span>
              )}
            </span>
            {catalog && (
              <span style={{ display: "flex", gap: "0.4rem" }}>
                <button
                  type="button"
                  className="ghost"
                  onClick={selectAllPermissions}
                  style={{ fontSize: "0.78rem" }}
                >
                  {t("selectAllScopes")}
                </button>
                <button
                  type="button"
                  className="ghost"
                  onClick={clearAllPermissions}
                  style={{ fontSize: "0.78rem" }}
                >
                  {t("clearAllScopes")}
                </button>
              </span>
            )}
          </div>
        </legend>
        {!grouped && <p className="meta">{t("loadingScopes")}</p>}
        {grouped &&
          CATEGORY_ORDER.map((cat) => {
            const entries = grouped.get(cat) ?? [];
            if (entries.length === 0) return null;
            return (
              <div
                key={cat}
                style={{
                  marginTop: "0.5rem",
                  padding: "0.4rem 0.6rem",
                  borderLeft:
                    cat === "danger"
                      ? "3px solid #c0392b"
                      : cat === "write"
                        ? "3px solid #d68910"
                        : "3px solid #28a745",
                  background: cat === "danger" ? "rgba(192,57,43,0.05)" : "transparent",
                }}
              >
                <div
                  className="meta"
                  style={{
                    fontSize: "0.75rem",
                    textTransform: "uppercase",
                    marginBottom: "0.2rem",
                  }}
                >
                  {t(`scopeCategory_${cat}`)}
                </div>
                {entries.map((entry) => (
                  <label
                    key={entry.key}
                    style={{
                      display: "flex",
                      alignItems: "flex-start",
                      gap: "0.5rem",
                      padding: "0.25rem 0",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={!!perms[entry.key]}
                      onChange={(e) => togglePermission(entry, e.target.checked)}
                      style={{ marginTop: "0.2rem" }}
                    />
                    <div>
                      <div>
                        <strong>{entry.label}</strong>{" "}
                        <code style={{ fontSize: "0.75rem", color: "var(--bv-meta, #666)" }}>
                          {entry.key}
                        </code>
                      </div>
                      {entry.description && (
                        <div className="meta" style={{ fontSize: "0.78rem" }}>
                          {entry.description}
                        </div>
                      )}
                    </div>
                  </label>
                ))}
              </div>
            );
          })}
      </fieldset>

      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.4rem",
          marginTop: "0.75rem",
          padding: "0.5rem",
          border: "1px solid var(--bv-card-border, #e5e7eb)",
          borderRadius: 4,
        }}
      >
        <input
          type="checkbox"
          checked={deidentify}
          onChange={(e) => setDeidentify(e.target.checked)}
        />
        <div>
          <strong>{t("deidentifyLabel")}</strong>
          <div className="meta" style={{ fontSize: "0.8rem" }}>
            {t("deidentifyHint")}
          </div>
        </div>
      </label>

      <div
        style={{ marginTop: "0.75rem", display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}
      >
        <button type="button" className="ghost" onClick={onCancel} disabled={busy}>
          {t("cancel")}
        </button>
        <button type="submit" disabled={busy}>
          {busy ? t("saving") : t("saveChanges")}
        </button>
      </div>
    </form>
  );
}

function AssistantCard({
  assistant,
  onEdit,
  onRotate,
  onSetActive,
  onDelete,
  onPatientsChanged,
}: {
  assistant: AiAssistant;
  onEdit: () => void;
  onRotate: () => void;
  onSetActive: (next: boolean) => void;
  onDelete: () => void;
  onPatientsChanged: () => Promise<void> | void;
}) {
  const t = useTranslations("aiShare");
  const [open, setOpen] = useState(false);
  const status = assistant.is_active
    ? { label: t("statusActive"), color: "#16a34a" }
    : { label: t("statusRevoked"), color: "#b42318" };

  return (
    <div className="card" style={{ marginBottom: "0.75rem" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "0.5rem",
        }}
      >
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
            <strong>{assistant.label}</strong>
            {assistant.provider && <span className="badge">{assistant.provider}</span>}
            {assistant.model_id && (
              <span className="badge" style={{ fontFamily: "monospace" }}>
                {assistant.model_id}
              </span>
            )}
            <span className="badge" style={{ background: status.color, color: "#fff" }}>
              {status.label}
            </span>
          </div>
          <div className="meta" style={{ fontSize: "0.8rem", marginTop: "0.2rem" }}>
            <code style={{ fontSize: "0.75rem" }}>{assistant.client_id}</code>
            {assistant.client_secret_prefix && (
              <>
                {" · "}
                <span title={t("secretPrefixHint")}>
                  secret{" "}
                  <code style={{ fontSize: "0.75rem" }}>{assistant.client_secret_prefix}…</code>
                </span>
              </>
            )}
            {" · "}
            {t("patientsCount", { n: assistant.patient_count })}
          </div>
        </div>
        <div style={{ display: "flex", gap: "0.35rem", flexShrink: 0 }}>
          <button type="button" className="ghost" onClick={() => setOpen((v) => !v)}>
            {open ? "▾" : "▸"} {t("patientsTitle", { label: "" }).trim()}
          </button>
          <button type="button" className="ghost" onClick={onEdit}>
            {t("editAssistant")}
          </button>
          <button type="button" className="ghost" onClick={onRotate}>
            {t("rotateSecret")}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => onSetActive(!assistant.is_active)}
            style={{
              color: assistant.is_active
                ? "var(--bv-danger, #b42318)"
                : "var(--bv-accent, #2563eb)",
            }}
          >
            {assistant.is_active ? t("revoke") : t("reactivate")}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={onDelete}
            style={{ color: "var(--bv-danger, #b42318)" }}
          >
            {t("deleteAssistant")}
          </button>
        </div>
      </div>

      {open && (
        <PatientShareList
          assistantId={assistant.id}
          assistantLabel={assistant.label}
          onChanged={onPatientsChanged}
        />
      )}
    </div>
  );
}

function CredentialsRevealCard({
  assistant,
  mcpUrl,
  onClose,
}: {
  assistant: AiAssistantCreated;
  mcpUrl: string;
  onClose: () => void;
}) {
  const t = useTranslations("aiShare");
  const [copied, setCopied] = useState<"" | "url" | "id" | "secret">("");

  const copy = useCallback(async (value: string, key: "url" | "id" | "secret") => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(key);
      setTimeout(() => setCopied(""), 2000);
    } catch {
      // ignore
    }
  }, []);

  return (
    <div
      className="card"
      style={{
        marginBottom: "1rem",
        padding: "1rem",
        background: "rgba(22,163,74,0.06)",
        borderColor: "var(--color-success, #16a34a)",
      }}
    >
      <h2 style={{ marginTop: 0 }}>{t("revealTitle")}</h2>
      <p style={{ marginTop: 0 }}>
        <strong>{t("revealWarningTitle")}</strong> {t("revealWarningBody")}
      </p>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr auto",
          gap: "0.4rem 0.6rem",
          alignItems: "center",
        }}
      >
        <span className="meta">{t("revealUrl")}</span>
        <code
          style={{
            background: "var(--bv-card-bg, #fff)",
            padding: "0.25rem 0.5rem",
            borderRadius: 4,
            wordBreak: "break-all",
          }}
        >
          {mcpUrl}
        </code>
        <button type="button" className="ghost" onClick={() => copy(mcpUrl, "url")}>
          {copied === "url" ? t("copied") : t("copy")}
        </button>

        <span className="meta">{t("revealClientId")}</span>
        <code
          style={{
            background: "var(--bv-card-bg, #fff)",
            padding: "0.25rem 0.5rem",
            borderRadius: 4,
            wordBreak: "break-all",
          }}
        >
          {assistant.client_id}
        </code>
        <button type="button" className="ghost" onClick={() => copy(assistant.client_id, "id")}>
          {copied === "id" ? t("copied") : t("copy")}
        </button>

        <span className="meta">{t("revealClientSecret")}</span>
        <code
          style={{
            background: "var(--bv-card-bg, #fff)",
            padding: "0.25rem 0.5rem",
            borderRadius: 4,
            wordBreak: "break-all",
          }}
        >
          {assistant.client_secret}
        </code>
        <button
          type="button"
          className="ghost"
          onClick={() => copy(assistant.client_secret, "secret")}
        >
          {copied === "secret" ? t("copied") : t("copy")}
        </button>
      </div>

      <p className="meta" style={{ marginTop: "0.75rem", fontSize: "0.8rem" }}>
        {t("revealHowTo")}
      </p>

      <div style={{ marginTop: "0.75rem", display: "flex", justifyContent: "flex-end" }}>
        <button type="button" onClick={onClose}>
          {t("revealAcknowledge")}
        </button>
      </div>
    </div>
  );
}

function PatientShareList({
  assistantId,
  assistantLabel,
  onChanged,
}: {
  assistantId: string;
  assistantLabel: string;
  onChanged: () => Promise<void> | void;
}) {
  const t = useTranslations("aiShare");
  const modal = useModal();
  const [patients, setPatients] = useState<AssistantSharedPatient[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const reload = useCallback(async () => {
    try {
      setPatients(await aiAssistantsApi.listPatients(assistantId));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [assistantId, t]);

  useEffect(() => {
    reload();
  }, [reload]);

  const remove = useCallback(
    async (p: AssistantSharedPatient) => {
      const ok = await modal.confirm({
        message: t("removePatientConfirm", { name: p.display_name }),
        destructive: true,
        confirmLabel: t("removePatient"),
      });
      if (!ok) return;
      try {
        await aiAssistantsApi.unsharePatient(assistantId, p.patient_id);
        await reload();
        await onChanged();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : t("actionFailed"));
      }
    },
    [assistantId, modal, onChanged, reload, t],
  );

  return (
    <div
      style={{
        marginTop: "0.75rem",
        borderTop: "1px solid var(--bv-card-border, #e5e7eb)",
        paddingTop: "0.75rem",
      }}
    >
      <h3 style={{ marginTop: 0, fontSize: "0.95rem" }}>
        {t("patientsTitle", { label: assistantLabel })}
      </h3>
      {err && <p className="error">{err}</p>}
      {patients === null && <p className="meta">Loading…</p>}
      {patients !== null && patients.length === 0 && (
        <p className="meta" style={{ fontSize: "0.85rem" }}>
          {t("patientsEmpty")}
        </p>
      )}
      {patients?.map((p) => (
        <div
          key={p.patient_id}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "0.3rem 0",
            borderBottom: "1px solid var(--bv-card-border, #f0f1f4)",
          }}
        >
          <Link href={`/patients/${p.patient_id}`}>{p.display_name}</Link>
          <button
            type="button"
            className="ghost"
            onClick={() => remove(p)}
            style={{ color: "var(--bv-danger, #b42318)", fontSize: "0.85rem" }}
          >
            ×
          </button>
        </div>
      ))}
      {!adding && (
        <button
          type="button"
          className="ghost"
          onClick={() => setAdding(true)}
          style={{ marginTop: "0.5rem", fontSize: "0.85rem" }}
        >
          + {t("addPatient")}
        </button>
      )}
      {adding && (
        <PatientPicker
          excludeIds={new Set((patients ?? []).map((p) => p.patient_id))}
          onPick={async (patientId) => {
            try {
              await aiAssistantsApi.sharePatient(assistantId, patientId);
              setAdding(false);
              await reload();
              await onChanged();
            } catch (e) {
              setErr(e instanceof ApiError ? e.message : t("actionFailed"));
            }
          }}
          onCancel={() => setAdding(false)}
        />
      )}
    </div>
  );
}

function PatientPicker({
  excludeIds,
  onPick,
  onCancel,
}: {
  excludeIds: Set<string>;
  onPick: (patientId: string) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("aiShare");
  const [q, setQ] = useState("");
  const [results, setResults] = useState<{ id: string; display_name: string }[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const id = setTimeout(async () => {
      if (q.trim().length < 2) {
        setResults([]);
        return;
      }
      setBusy(true);
      try {
        const { patientsApi } = await import("@/lib/api");
        const resp = await patientsApi.list({ q: q.trim(), scope: "all" });
        if (!cancelled) {
          setResults(
            resp.items
              .filter((p) => !excludeIds.has(p.id))
              .map((p) => ({ id: p.id, display_name: p.display_name })),
          );
        }
      } finally {
        if (!cancelled) setBusy(false);
      }
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [q, excludeIds]);

  return (
    <div style={{ marginTop: "0.5rem" }}>
      <div style={{ display: "flex", gap: "0.4rem" }}>
        <input
          // biome-ignore lint/a11y/noAutofocus: picker mounts when the user clicks "add patient"; focus is the intentional continuation of that click.
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("addPatientPicker")}
          style={{ flex: 1 }}
        />
        <button type="button" className="ghost" onClick={onCancel}>
          {t("cancel")}
        </button>
      </div>
      {q.trim().length >= 2 && !busy && results.length === 0 && (
        <p className="meta" style={{ fontSize: "0.8rem", marginTop: "0.3rem" }}>
          {t("noPatientMatches")}
        </p>
      )}
      {results.map((r) => (
        <button
          key={r.id}
          type="button"
          className="ghost"
          onClick={() => onPick(r.id)}
          style={{
            display: "block",
            width: "100%",
            textAlign: "left",
            padding: "0.3rem 0.5rem",
            marginTop: "0.2rem",
            fontSize: "0.85rem",
          }}
        >
          {r.display_name}
        </button>
      ))}
    </div>
  );
}
