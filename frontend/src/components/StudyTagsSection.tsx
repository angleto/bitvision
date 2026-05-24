"use client";

// Tag editor for the study detail page.
//
// Backend has Tag(target_kind='study', target_id, namespace, value, source).
// Auto-tags emitted by workers carry source='auto' and are visually
// dimmed; manual rows are full-opacity. Removal hits DELETE /tags/{id}
// and is gated on write:annotations on the parent study.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useModal } from "@/components/ModalHost";
import { ApiError, type Tag, tagsApi } from "@/lib/api";

interface Props {
  studyId: string;
  canWrite: boolean;
}

const NAMESPACE_KEYS: { value: string; key: string }[] = [
  { value: "clinical", key: "nsClinical" },
  { value: "anatomy", key: "nsAnatomy" },
  { value: "modality", key: "nsModality" },
  { value: "episode", key: "nsEpisode" },
  { value: "custom", key: "nsCustom" },
];

export default function StudyTagsSection({ studyId, canWrite }: Props) {
  const tTags = useTranslations("studyTags");
  const modal = useModal();
  const [tags, setTags] = useState<Tag[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [namespace, setNamespace] = useState("clinical");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [suggestions, setSuggestions] = useState<Tag[]>([]);

  const refresh = useCallback(async () => {
    try {
      const rows = await tagsApi.forTarget("study", studyId);
      setTags(rows);
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [studyId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Lightweight namespace-scoped autocomplete: hits GET /tags as the user types.
  useEffect(() => {
    const v = value.trim();
    if (!v) {
      setSuggestions([]);
      return;
    }
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        const rows = await tagsApi.list({ namespace, q: v, limit: 8 });
        if (!cancelled) setSuggestions(rows);
      } catch {
        if (!cancelled) setSuggestions([]);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [value, namespace]);

  async function handleAdd(rawValue?: string) {
    const v = (rawValue ?? value).trim();
    if (!v || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await tagsApi.add({
        target_kind: "study",
        target_id: studyId,
        namespace,
        value: v,
      });
      setValue("");
      setSuggestions([]);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "add failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove(tagId: string) {
    const ok = await modal.confirm({
      message: tTags("removeConfirm"),
      destructive: true,
      confirmLabel: tTags("removeBtn"),
    });
    if (!ok) return;
    try {
      await tagsApi.remove(tagId);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "remove failed");
    }
  }

  const grouped = (tags ?? []).reduce<Record<string, Tag[]>>((acc, t) => {
    let bucket = acc[t.namespace];
    if (!bucket) {
      bucket = [];
      acc[t.namespace] = bucket;
    }
    bucket.push(t);
    return acc;
  }, {});
  const orderedNs = Object.keys(grouped).sort();

  return (
    <section style={{ marginTop: "2rem" }}>
      <h2>{tTags("sectionTitle")}</h2>
      <p className="meta" style={{ marginTop: "-0.4rem" }}>
        {tTags("intro", { example: "post-operatorio-2025" })}
      </p>

      {err && <p className="error">{err}</p>}

      {tags === null ? (
        <p className="meta">{tTags("loading")}</p>
      ) : tags.length === 0 ? (
        <p className="meta">{tTags("empty")}</p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          {orderedNs.map((ns) => (
            <div
              key={ns}
              style={{ display: "flex", alignItems: "baseline", gap: "0.5rem", flexWrap: "wrap" }}
            >
              <span className="meta" style={{ minWidth: "5.5rem", fontWeight: 600 }}>
                {ns}
              </span>
              <span className="badges" style={{ display: "flex", flexWrap: "wrap", gap: "0.3rem" }}>
                {grouped[ns].map((t) => {
                  const isAuto = t.source === "auto";
                  return (
                    <span
                      key={t.id}
                      className="badge"
                      title={
                        isAuto
                          ? tTags("autoTagTitle", {
                              confidence: t.confidence ?? "?",
                            })
                          : tTags("manualTagTitle")
                      }
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "0.25rem",
                        opacity: isAuto ? 0.7 : 1,
                        fontStyle: isAuto ? "italic" : "normal",
                      }}
                    >
                      {t.value}
                      {canWrite && (
                        <button
                          type="button"
                          onClick={() => handleRemove(t.id)}
                          aria-label={tTags("removeTagAria", {
                            ns,
                            value: t.value,
                          })}
                          style={{
                            background: "transparent",
                            color: "inherit",
                            border: "none",
                            padding: 0,
                            marginLeft: "0.15rem",
                            fontSize: "0.95rem",
                            lineHeight: 1,
                            cursor: "pointer",
                          }}
                        >
                          ×
                        </button>
                      )}
                    </span>
                  );
                })}
              </span>
            </div>
          ))}
        </div>
      )}

      {canWrite && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleAdd();
          }}
          style={{
            marginTop: "0.9rem",
            display: "flex",
            gap: "0.4rem",
            alignItems: "stretch",
            flexWrap: "wrap",
            position: "relative",
          }}
        >
          <select
            value={namespace}
            onChange={(e) => setNamespace(e.target.value)}
            style={{ minWidth: "11rem" }}
          >
            {NAMESPACE_KEYS.map((n) => (
              <option key={n.value} value={n.value}>
                {tTags(n.key)}
              </option>
            ))}
          </select>
          <input
            type="text"
            placeholder={tTags("valuePlaceholder")}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            style={{ flex: 1, minWidth: "14rem" }}
          />
          <button type="submit" disabled={busy || !value.trim()}>
            {busy ? tTags("addBusy") : tTags("addBtn")}
          </button>
          {suggestions.length > 0 && value.trim() && (
            // Use <div> (not <ul>) so biome doesn't conflict between
            // ``role="listbox"`` and the ul's non-interactive default
            // role; the input above drives keyboard navigation.
            <div
              // biome-ignore lint/a11y/useSemanticElements: custom combobox listbox; native <select> cannot host the inline filter / chip UI
              role="listbox"
              tabIndex={-1}
              style={{
                position: "absolute",
                top: "100%",
                left: "11.5rem",
                zIndex: 30,
                margin: "0.2rem 0 0",
                padding: "0.2rem 0",
                listStyle: "none",
                background: "var(--bv-card-bg, #fff)",
                border: "1px solid var(--bv-card-border, #e5e7eb)",
                borderRadius: 6,
                maxHeight: "12rem",
                overflowY: "auto",
                minWidth: "16rem",
                boxShadow: "0 4px 10px rgba(0,0,0,0.08)",
              }}
            >
              {suggestions.map((s, i) => (
                <div
                  key={`${s.namespace}:${s.value}-${i}`}
                  // biome-ignore lint/a11y/useSemanticElements: custom combobox option inside a non-<select> listbox
                  role="option"
                  aria-selected={false}
                  tabIndex={-1}
                  // onMouseDown so the click registers before the input loses focus.
                  onMouseDown={(e) => {
                    e.preventDefault();
                    handleAdd(s.value);
                  }}
                  style={{ padding: "0.3rem 0.7rem", cursor: "pointer" }}
                >
                  {s.value}
                </div>
              ))}
            </div>
          )}
        </form>
      )}
    </section>
  );
}
