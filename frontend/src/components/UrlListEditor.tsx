"use client";

// Minimal editor for an array of ``{label, url, mime?, size?}``. Used
// by PlanEventDialog and EditEventDialog for the ``links`` and
// ``attachments`` event fields. Behaviour is identical for both;
// the parent decides which one it maps to.
//
// Empty rows (no url) are filtered out by the parent before submit.

import type { CSSProperties } from "react";

import type { ClinicalEvent } from "@/lib/api_records";

type LinkItem = NonNullable<ClinicalEvent["links"]>[number];
// Kept generic so the editor can grow extra optional keys (mime, size,
// kind, etc.) without changing the callsites — used by ``links`` and
// any future URL-list field.
export type UrlItem = LinkItem & { mime?: string; size?: number };

interface Props {
  items: UrlItem[];
  onChange: (next: UrlItem[]) => void;
  addLabel: string;
  // When true, show mime/size columns. Attachments only.
  withMeta?: boolean;
  placeholderLabel: string;
  placeholderUrl: string;
}

export default function UrlListEditor({
  items,
  onChange,
  addLabel,
  withMeta = false,
  placeholderLabel,
  placeholderUrl,
}: Props) {
  function update(i: number, patch: Partial<UrlItem>): void {
    const next = items.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  }
  function add(): void {
    onChange([...items, { label: "", url: "" }]);
  }
  function remove(i: number): void {
    onChange(items.filter((_, j) => j !== i));
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {items.map((it, i) => (
        <div
          key={`${it.url}-${i}`}
          style={{
            display: "grid",
            gridTemplateColumns: withMeta ? "1fr 2fr 80px 80px 28px" : "1fr 2fr 28px",
            gap: 4,
            alignItems: "center",
          }}
        >
          <input
            type="text"
            value={it.label ?? ""}
            onChange={(e) => update(i, { label: e.target.value })}
            placeholder={placeholderLabel}
            style={inputStyle}
            maxLength={120}
          />
          <input
            type="url"
            value={it.url ?? ""}
            onChange={(e) => update(i, { url: e.target.value })}
            placeholder={placeholderUrl}
            style={inputStyle}
          />
          {withMeta && (
            <>
              <input
                type="text"
                value={it.mime ?? ""}
                onChange={(e) => update(i, { mime: e.target.value } as Partial<UrlItem>)}
                placeholder="mime"
                style={inputStyle}
                maxLength={80}
              />
              <input
                type="number"
                min={0}
                value={it.size ?? ""}
                onChange={(e) =>
                  update(i, {
                    size: e.target.value ? Number(e.target.value) : undefined,
                  } as Partial<UrlItem>)
                }
                placeholder="bytes"
                style={inputStyle}
              />
            </>
          )}
          <button
            type="button"
            className="ghost"
            onClick={() => remove(i)}
            aria-label="Remove"
            style={{ fontSize: "0.9rem", padding: 0, width: 28, height: 28 }}
          >
            ✕
          </button>
        </div>
      ))}
      <button
        type="button"
        className="ghost"
        onClick={add}
        style={{
          fontSize: "0.78rem",
          padding: "0.2rem 0.6rem",
          alignSelf: "flex-start",
          marginTop: 2,
        }}
      >
        + {addLabel}
      </button>
    </div>
  );
}

const inputStyle: CSSProperties = {
  fontSize: "0.82rem",
  padding: "0.25rem 0.4rem",
  border: "1px solid var(--bv-card-border, #d0d5dd)",
  borderRadius: 6,
  background: "var(--bv-card-bg, transparent)",
  color: "var(--bv-fg)",
  width: "100%",
};
