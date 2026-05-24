"use client";

// /admin/llm-prompts — dedicated UI to override the Q&A system prompt
// per locale, with a side-by-side diff against the in-code default and
// a one-click restore. The wire format is the existing app_settings
// row (key ``qna.system_prompt.<locale>``); this page just wraps it in
// affordances the generic /admin/settings table cannot offer:
//
//   - one large textarea per locale (40+ rows)
//   - status pill (default / override active / unsaved)
//   - diff vs the frozen default (line-based, computed client-side)
//   - "Restore default" with confirmation
//   - read-only view of the original default for reference

import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, type LlmPromptEntry, adminLlmPromptsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

type LocaleCode = string;
type ViewMode = "editor" | "diff" | "default";

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function localeLabel(code: string, t: (k: string) => string): string {
  if (code === "it") return t("localeIT");
  if (code === "en") return t("localeEN");
  return code.toUpperCase();
}

type DiffLine = {
  kind: "same" | "add" | "del";
  text: string;
};

// Trivial LCS-based line diff. Inputs are < 32k char each and split by
// newline; the quadratic complexity is fine for this size and dodges
// the dependency on an external diff library.
function diffLines(a: string, b: string): DiffLine[] {
  const A = a.split("\n");
  const B = b.split("\n");
  const n = A.length;
  const m = B.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (A[i] === B[j]) {
        lcs[i][j] = lcs[i + 1][j + 1] + 1;
      } else {
        lcs[i][j] = Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) {
      out.push({ kind: "same", text: A[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "del", text: A[i] });
      i++;
    } else {
      out.push({ kind: "add", text: B[j] });
      j++;
    }
  }
  while (i < n) out.push({ kind: "del", text: A[i++] });
  while (j < m) out.push({ kind: "add", text: B[j++] });
  return out;
}

function diffSummary(lines: DiffLine[]) {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.kind === "add") added++;
    else if (l.kind === "del") removed++;
  }
  // Lines that appear as a deletion immediately followed by an
  // insertion count as a single "changed" row for the summary; this
  // is purely cosmetic but matches how a human would read the diff.
  let changed = 0;
  for (let i = 0; i < lines.length - 1; i++) {
    if (lines[i].kind === "del" && lines[i + 1].kind === "add") {
      changed++;
      i++; // skip the paired add
    }
  }
  return { added: added - changed, removed: removed - changed, changed };
}

export default function AdminLlmPromptsPage() {
  const { user, status } = useAuth();
  const router = useRouter();
  const t = useTranslations("adminLlmPrompts");

  const [entries, setEntries] = useState<LlmPromptEntry[] | null>(null);
  const [activeLocale, setActiveLocale] = useState<LocaleCode | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [view, setView] = useState<ViewMode>("editor");
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  useEffect(() => {
    if (status !== "ready") return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!user.is_admin) {
      router.replace("/");
    }
  }, [status, user, router]);

  // ``refresh`` only depends on the translator. The "first locale
  // wins" initialisation is folded inside via a state updater so we
  // do not pull ``activeLocale`` into the dep array; otherwise every
  // tab click would re-fetch the bundle.
  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const data = await adminLlmPromptsApi.list();
      setEntries(data);
      const next: Record<string, string> = {};
      for (const e of data) next[e.locale] = e.current_text;
      setDraft(next);
      setActiveLocale((prev) => prev ?? (data.length > 0 ? data[0].locale : null));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    if (status === "ready" && user?.is_admin) refresh();
  }, [status, user?.is_admin, refresh]);

  const active = useMemo(
    () => entries?.find((e) => e.locale === activeLocale) ?? null,
    [entries, activeLocale],
  );
  const draftText = active ? (draft[active.locale] ?? "") : "";
  const isDirty = active ? draftText !== active.current_text : false;

  const diff = useMemo(() => {
    if (!active) return [];
    return diffLines(active.default_text, draftText);
  }, [active, draftText]);
  const diffStats = useMemo(() => diffSummary(diff), [diff]);
  const hasDiff = diff.some((l) => l.kind !== "same");

  async function onSave() {
    if (!active) return;
    setSaving(active.locale);
    setErr(null);
    setInfo(null);
    try {
      await adminLlmPromptsApi.update(active.locale, draftText);
      setInfo(t("saved"));
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("saveFailed"));
    } finally {
      setSaving(null);
    }
  }

  async function onReset() {
    if (!active) return;
    const label = localeLabel(active.locale, t);
    if (!window.confirm(t("resetConfirm", { locale: label }))) return;
    setSaving(active.locale);
    setErr(null);
    setInfo(null);
    try {
      await adminLlmPromptsApi.reset(active.locale);
      setInfo(t("resetDone"));
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("saveFailed"));
    } finally {
      setSaving(null);
    }
  }

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

  return (
    <main style={{ padding: "1.25rem", maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ marginBottom: "0.25rem" }}>{t("title")}</h1>
      <p className="meta" style={{ marginTop: 0, marginBottom: "1rem" }}>
        {t("subtitle")}
      </p>

      {err && (
        <p className="error" role="alert" style={{ marginBottom: "0.6rem" }}>
          {err}
        </p>
      )}
      {info && (
        <p
          aria-live="polite"
          style={{
            marginBottom: "0.6rem",
            color: "var(--bv-success, #2c8a4d)",
            fontWeight: 500,
          }}
        >
          {info}
        </p>
      )}

      {entries && entries.length > 0 && (
        <>
          {/* locale switcher */}
          <div
            role="tablist"
            aria-label={t("title")}
            style={{
              display: "flex",
              gap: "0.4rem",
              borderBottom: "1px solid var(--bv-card-border)",
              marginBottom: "0.8rem",
            }}
          >
            {entries.map((e) => {
              const isActive = e.locale === activeLocale;
              return (
                <button
                  key={e.locale}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => {
                    setActiveLocale(e.locale);
                    setView("editor");
                    setInfo(null);
                  }}
                  style={{
                    background: "transparent",
                    border: "none",
                    borderBottom: isActive
                      ? "2px solid var(--bv-accent, #0a84ff)"
                      : "2px solid transparent",
                    padding: "0.45rem 0.9rem",
                    fontWeight: isActive ? 600 : 400,
                    color: "var(--bv-fg)",
                    opacity: isActive ? 1 : 0.7,
                    cursor: "pointer",
                  }}
                >
                  {localeLabel(e.locale, t)}
                </button>
              );
            })}
          </div>

          {active && (
            <>
              {/* status pill row */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: "0.6rem",
                  marginBottom: "0.6rem",
                }}
              >
                <StatusPill
                  isOverride={active.is_override}
                  isDirty={isDirty}
                  t={t as (k: string) => string}
                />
                {active.is_override && (
                  <span className="meta" style={{ fontSize: "0.78rem" }}>
                    {t("metaUpdatedAt", { at: formatDate(active.updated_at) })}
                    {active.updated_by_subject_id && (
                      <>
                        {" · "}
                        {t("metaUpdatedBy", { who: active.updated_by_subject_id.slice(0, 8) })}
                      </>
                    )}
                  </span>
                )}
                <span className="meta" style={{ fontSize: "0.78rem", marginLeft: "auto" }}>
                  {t("lengthLabel", { n: draftText.length })}
                </span>
              </div>

              {/* view-mode switcher (editor / diff / reference) */}
              <div
                role="tablist"
                aria-label={t("tabEditor")}
                style={{
                  display: "flex",
                  gap: "0.3rem",
                  marginBottom: "0.5rem",
                }}
              >
                {(["editor", "diff", "default"] as ViewMode[]).map((m) => {
                  const isActive = m === view;
                  return (
                    <button
                      key={m}
                      type="button"
                      role="tab"
                      aria-selected={isActive}
                      onClick={() => setView(m)}
                      style={{
                        background: isActive
                          ? "var(--bv-card-bg-soft, var(--bv-card-bg))"
                          : "transparent",
                        border: "1px solid var(--bv-card-border)",
                        borderRadius: "var(--bv-r-sm, 4px)",
                        padding: "0.3rem 0.7rem",
                        fontSize: "0.82rem",
                        cursor: "pointer",
                        fontWeight: isActive ? 600 : 400,
                        color: "var(--bv-fg)",
                      }}
                    >
                      {m === "editor"
                        ? t("tabEditor")
                        : m === "diff"
                          ? t("tabPreview")
                          : t("tabReference")}
                    </button>
                  );
                })}
              </div>

              {view === "editor" && (
                <>
                  <textarea
                    value={draftText}
                    onChange={(ev) =>
                      setDraft((prev) => ({ ...prev, [active.locale]: ev.target.value }))
                    }
                    spellCheck={false}
                    rows={28}
                    maxLength={32_000}
                    style={{
                      width: "100%",
                      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                      fontSize: "0.86rem",
                      lineHeight: 1.5,
                      padding: "0.7rem",
                      border: "1px solid var(--bv-input-border)",
                      borderRadius: "var(--bv-r-sm, 4px)",
                      background: "var(--bv-input-bg, var(--bv-card-bg))",
                      color: "var(--bv-fg)",
                      resize: "vertical",
                    }}
                  />
                  <p className="meta" style={{ marginTop: "0.4rem", fontSize: "0.76rem" }}>
                    {t("editorHint")}
                  </p>
                </>
              )}

              {view === "diff" && (
                <div
                  style={{
                    border: "1px solid var(--bv-card-border)",
                    borderRadius: "var(--bv-r-sm, 4px)",
                    background: "var(--bv-card-bg-soft, var(--bv-card-bg))",
                    padding: "0.6rem 0",
                    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                    fontSize: "0.82rem",
                    maxHeight: 560,
                    overflow: "auto",
                  }}
                >
                  {!hasDiff && (
                    <p className="meta" style={{ padding: "0.6rem 0.8rem", margin: 0 }}>
                      {t("diffNone")}
                    </p>
                  )}
                  {hasDiff && (
                    <>
                      <p
                        className="meta"
                        style={{
                          padding: "0 0.8rem 0.4rem",
                          margin: 0,
                          fontSize: "0.76rem",
                          fontFamily: "system-ui, sans-serif",
                        }}
                      >
                        {t("diffSummary", diffStats)}
                      </p>
                      {diff.map((line, idx) => (
                        <div
                          // biome-ignore lint/suspicious/noArrayIndexKey: stable order, identity is positional
                          key={idx}
                          style={{
                            padding: "0.05rem 0.8rem",
                            background:
                              line.kind === "add"
                                ? "rgba(44, 138, 77, 0.12)"
                                : line.kind === "del"
                                  ? "rgba(207, 110, 110, 0.12)"
                                  : "transparent",
                            color:
                              line.kind === "add"
                                ? "var(--bv-success, #2c8a4d)"
                                : line.kind === "del"
                                  ? "var(--bv-error, #cf6e6e)"
                                  : "inherit",
                            whiteSpace: "pre-wrap",
                            wordBreak: "break-word",
                          }}
                        >
                          <span
                            style={{
                              display: "inline-block",
                              width: "1.2em",
                              opacity: 0.7,
                            }}
                          >
                            {line.kind === "add" ? "+" : line.kind === "del" ? "−" : " "}
                          </span>
                          {line.text || " "}
                        </div>
                      ))}
                    </>
                  )}
                </div>
              )}

              {view === "default" && (
                <pre
                  style={{
                    border: "1px solid var(--bv-card-border)",
                    borderRadius: "var(--bv-r-sm, 4px)",
                    background: "var(--bv-card-bg-soft, var(--bv-card-bg))",
                    padding: "0.8rem",
                    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                    fontSize: "0.82rem",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    maxHeight: 560,
                    overflow: "auto",
                  }}
                >
                  {active.default_text}
                </pre>
              )}

              <div
                style={{
                  display: "flex",
                  gap: "0.5rem",
                  justifyContent: "flex-end",
                  marginTop: "0.9rem",
                }}
              >
                <button
                  type="button"
                  onClick={onReset}
                  disabled={!active.is_override || saving === active.locale}
                  style={{
                    background: "transparent",
                    border: "1px solid var(--bv-card-border)",
                    color: "var(--bv-fg)",
                    padding: "0.45rem 0.9rem",
                    borderRadius: "var(--bv-r-sm, 4px)",
                    cursor: active.is_override ? "pointer" : "not-allowed",
                    opacity: active.is_override ? 1 : 0.5,
                  }}
                >
                  {t("reset")}
                </button>
                <button
                  type="button"
                  onClick={onSave}
                  disabled={!isDirty || saving === active.locale || draftText.trim().length === 0}
                  style={{
                    background: "var(--bv-accent, #0a84ff)",
                    border: "1px solid var(--bv-accent, #0a84ff)",
                    color: "#fff",
                    padding: "0.45rem 0.9rem",
                    borderRadius: "var(--bv-r-sm, 4px)",
                    cursor: isDirty ? "pointer" : "not-allowed",
                    opacity: isDirty ? 1 : 0.5,
                    fontWeight: 500,
                  }}
                >
                  {saving === active.locale ? "…" : t("save")}
                </button>
              </div>
            </>
          )}
        </>
      )}
    </main>
  );
}

function StatusPill({
  isOverride,
  isDirty,
  t,
}: {
  isOverride: boolean;
  isDirty: boolean;
  t: (k: string) => string;
}) {
  const label = isDirty
    ? t("statusUnsaved")
    : isOverride
      ? t("statusOverride")
      : t("statusDefault");
  const color = isDirty
    ? "var(--bv-warning, #c98700)"
    : isOverride
      ? "var(--bv-accent, #0a84ff)"
      : "var(--bv-success, #2c8a4d)";
  return (
    <span
      style={{
        display: "inline-block",
        fontSize: "0.72rem",
        fontWeight: 600,
        padding: "0.18rem 0.55rem",
        borderRadius: 999,
        border: `1px solid ${color}`,
        color,
        background: "transparent",
      }}
    >
      {label}
    </span>
  );
}
