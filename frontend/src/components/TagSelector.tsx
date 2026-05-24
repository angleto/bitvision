"use client";

// Reusable controlled tag-selector widget.
//
// The `value` prop is the current list of "namespace:value" strings; the
// parent is responsible for persisting them. Typing into the input debounces
// by 300ms and hits GET /api/tags?q=… for autocomplete suggestions. Enter
// commits either the highlighted suggestion or the raw typed text (if well
// formed, i.e. contains a colon). Backspace on an empty input pops the last
// chip, matching the standard chip-input UX.
//
// Usage examples (not implemented here):
//   - UploadPage:       manual tags on a brand-new study
//   - StudyDetailPage:  "add tag" action next to the existing tag list
//   - Search filter bar: refine /studies listing by structured tag
//
// Example:
//   const [tags, setTags] = useState<string[]>([]);
//   <TagSelector value={tags} onChange={setTags} namespaces={["anatomy", "modality"]} />

import { useEffect, useRef, useState } from "react";

import { ApiError, type Tag, tagsApi } from "@/lib/api";

interface Props {
  value: string[];
  onChange: (next: string[]) => void;
  /** Restrict autocomplete suggestions to these namespaces. Empty = all. */
  namespaces?: string[];
  placeholder?: string;
}

const DEBOUNCE_MS = 300;

export default function TagSelector({
  value,
  onChange,
  namespaces,
  placeholder = "Add tag (namespace:value)…",
}: Props) {
  const [input, setInput] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [highlight, setHighlight] = useState(0);
  const [openMenu, setOpenMenu] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const raw = input.trim();
    if (!raw) {
      setSuggestions([]);
      setOpenMenu(false);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const [nsPart, ...rest] = raw.split(":");
        const hasColon = raw.includes(":");
        const q = hasColon ? rest.join(":") : raw;
        const ns = hasColon && (!namespaces || namespaces.includes(nsPart)) ? nsPart : undefined;

        const requests = ns
          ? [tagsApi.list({ namespace: ns, q })]
          : namespaces?.length
            ? namespaces.map((n) => tagsApi.list({ namespace: n, q }))
            : [tagsApi.list({ q })];
        const rows = await Promise.all(requests);
        if (cancelled) return;

        const selected = new Set(value);
        const seen = new Set<string>();
        const out: string[] = [];
        for (const batch of rows) {
          for (const row of batch as Tag[]) {
            const key = `${row.namespace}:${row.value}`;
            if (seen.has(key) || selected.has(key)) continue;
            seen.add(key);
            out.push(key);
          }
        }
        setSuggestions(out.slice(0, 20));
        setHighlight(0);
        setOpenMenu(true);
        setErr(null);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "lookup failed");
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [input, namespaces, value]);

  const commit = (candidate: string) => {
    const c = candidate.trim();
    if (!c || !c.includes(":") || value.includes(c)) return;
    onChange([...value, c]);
    setInput("");
    setSuggestions([]);
    setOpenMenu(false);
  };

  const removeAt = (idx: number) => {
    const next = value.slice();
    next.splice(idx, 1);
    onChange(next);
  };

  return (
    <div className="tag-selector" style={{ position: "relative" }}>
      {/* Wrapper is a "click anywhere to focus the embedded input"
          affordance; keyboard users tab directly to the <input>, so
          the wrapper has no separate keyboard handler. */}
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: focus delegation only; the inner <input> is the real keyboard target. */}
      <div
        onClick={() => inputRef.current?.focus()}
        role="presentation"
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.3rem",
          alignItems: "center",
          padding: "0.3rem 0.45rem",
          border: "1px solid var(--bv-input-border, #d0d5dd)",
          borderRadius: 6,
          background: "var(--bv-input-bg, #fff)",
          minHeight: "2.2rem",
        }}
      >
        {value.map((tag, i) => (
          <span
            // ``commit`` rejects duplicates (``value.includes(c)``), so
            // the tag string is unique for the lifetime of the row.
            key={tag}
            className="badge"
            style={{ display: "inline-flex", alignItems: "center", gap: "0.25rem" }}
          >
            {tag}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                removeAt(i);
              }}
              aria-label={`Remove ${tag}`}
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
          </span>
        ))}
        <input
          ref={inputRef}
          type="text"
          value={input}
          placeholder={value.length === 0 ? placeholder : ""}
          onChange={(e) => setInput(e.target.value)}
          onFocus={() => suggestions.length && setOpenMenu(true)}
          onBlur={() => setTimeout(() => setOpenMenu(false), 150)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              const picked = openMenu && suggestions[highlight] ? suggestions[highlight] : input;
              commit(picked);
            } else if (e.key === "Backspace" && input === "" && value.length > 0) {
              e.preventDefault();
              removeAt(value.length - 1);
            } else if (e.key === "ArrowDown" && suggestions.length) {
              e.preventDefault();
              setOpenMenu(true);
              setHighlight((h) => (h + 1) % suggestions.length);
            } else if (e.key === "ArrowUp" && suggestions.length) {
              e.preventDefault();
              setOpenMenu(true);
              setHighlight((h) => (h - 1 + suggestions.length) % suggestions.length);
            } else if (e.key === "Escape") {
              setOpenMenu(false);
            }
          }}
          style={{
            flex: 1,
            minWidth: "10ch",
            border: "none",
            outline: "none",
            padding: "0.1rem 0.2rem",
            background: "transparent",
            color: "inherit",
          }}
        />
      </div>
      {openMenu && suggestions.length > 0 && (
        // Combobox listbox: <div> (not <ul>) so biome's
        // noNoninteractiveElementToInteractiveRole stays quiet —
        // <ul role="listbox"> conflicts with the rule because the
        // ul element itself has a non-interactive default role.
        // Visually identical; the children are the option entries.
        <div
          // biome-ignore lint/a11y/useSemanticElements: custom combobox listbox; native <select> cannot host the inline filter / chip UI
          role="listbox"
          // Keyboard interactions live on the <input> above (Arrow,
          // Enter, Esc); the listbox itself is mouse-only, so
          // ``tabIndex={-1}`` keeps it focusable for screen-reader
          // navigation without putting it in the natural tab order.
          tabIndex={-1}
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            zIndex: 30,
            listStyle: "none",
            margin: "0.2rem 0 0",
            padding: "0.2rem 0",
            background: "var(--bv-card-bg, #fff)",
            border: "1px solid var(--bv-card-border, #e5e7eb)",
            borderRadius: 6,
            maxHeight: "12rem",
            overflowY: "auto",
            boxShadow: "0 4px 10px rgba(0,0,0,0.08)",
          }}
        >
          {suggestions.map((s, i) => (
            <div
              key={s}
              // biome-ignore lint/a11y/useSemanticElements: custom combobox option inside a non-<select> listbox
              role="option"
              aria-selected={i === highlight}
              // ``tabIndex={-1}`` keeps the option out of the tab
              // order (the input above is the focusable affordance —
              // Arrow / Enter drive selection via ``aria-activedescendant``)
              // but still satisfies ``useFocusableInteractive``.
              tabIndex={-1}
              // onMouseDown fires before input blur — crucial so the click
              // isn't cancelled by the blur handler that closes the menu.
              onMouseDown={(e) => {
                e.preventDefault();
                commit(s);
              }}
              onMouseEnter={() => setHighlight(i)}
              style={{
                padding: "0.3rem 0.7rem",
                cursor: "pointer",
                background: i === highlight ? "rgba(233,107,31,0.12)" : "transparent",
              }}
            >
              {s}
            </div>
          ))}
        </div>
      )}
      {err && (
        <p className="meta" style={{ color: "#b42318", marginTop: "0.3rem" }}>
          {err}
        </p>
      )}
    </div>
  );
}
