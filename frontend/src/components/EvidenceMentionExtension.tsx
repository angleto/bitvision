"use client";

// TipTap extension that turns the ``@`` keystroke into a suggestion
// dropdown for picking a study / series / folder / document / report /
// consultation / tag of the active patient. The selection is inserted
// as plain text (``@kind:UUID`` or ``@tag:value``) so the existing
// markdown round-trip stays intact: the read-side parser
// (``services/evidence_links.py`` and the ``EvidenceContent``
// renderer) reads exactly what the editor wrote.
//
// Differences from a stock ``@tiptap/extension-mention``:
//
// * No custom node — the mention is plain text, so tiptap-markdown
//   doesn't need a custom serializer.
// * Patient-scoped lookup — every search call is ``/api/patients/
//   {pid}/search`` so cross-patient bleed is impossible at the data
//   source. The persist-time guard
//   (``services/evidence_links.validate_mentions_or_raise``) is a
//   second belt: even if the user types a UUID by hand, the save
//   fails when the resource isn't theirs.
//
// Trigger UX:
//
//   @         → dropdown shows recent / matching items across all kinds
//   @study    → narrows to studies (kind prefix filter)
//   @doc      → narrows to documents
//   @tag:liv  → matches existing tags by prefix
//
// Pure plain-text approach — no custom node — keeps tiptap-markdown's
// serializer happy.

import { type Editor, Extension } from "@tiptap/core";
import { ReactRenderer } from "@tiptap/react";
import Suggestion, { type SuggestionOptions } from "@tiptap/suggestion";
import {
  type ForwardedRef,
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useState,
} from "react";

import { type PatientSearchSection, patientSearchApi } from "@/lib/api";

export type EvidenceSuggestionKind =
  | "study"
  | "series"
  | "folder"
  | "document"
  | "consultation"
  | "report"
  | "tag";

export interface EvidenceSuggestionItem {
  kind: EvidenceSuggestionKind;
  /** UUID for resource kinds, value string for ``tag``. */
  id: string;
  label: string;
  /** Optional secondary label (date / author / preview). */
  hint?: string | null;
}

const KIND_FROM_SECTION: Record<PatientSearchSection, EvidenceSuggestionKind> = {
  studies: "study",
  reports: "report",
  documents: "document",
  consultations: "consultation",
  folders: "folder",
  // ``annotations`` is mapped to ``report`` for now: we don't currently
  // expose annotations as a stand-alone mention target. Annotations
  // already roll up into a study, which can be referenced via @study.
  annotations: "report",
};

// Order in the dropdown when the search returns multiple kinds.
const KIND_ORDER: EvidenceSuggestionKind[] = [
  "study",
  "series",
  "folder",
  "document",
  "report",
  "consultation",
  "tag",
];

// Italian / English prefixes the user is likely to type AFTER ``@`` to
// scope the autocomplete to a single kind. Listed longest-first so
// "studio" wins over "stu" — the items() callback walks the array
// linearly and stops at the first match.
//
// Each entry maps a prefix to the backend section name used by the
// ``mention-search`` endpoint's ``sections`` query param.
const KIND_PREFIXES: ReadonlyArray<[string, PatientSearchSection]> = [
  ["studies", "studies"],
  ["studio", "studies"],
  ["studi", "studies"],
  ["study", "studies"],
  ["stud", "studies"],
  ["referti", "reports"],
  ["referto", "reports"],
  ["report", "reports"],
  ["referto", "reports"],
  ["refert", "reports"],
  ["rep", "reports"],
  ["documenti", "documents"],
  ["documento", "documents"],
  ["document", "documents"],
  ["docum", "documents"],
  ["doc", "documents"],
  ["consulti", "consultations"],
  ["consulto", "consultations"],
  ["consultations", "consultations"],
  ["consultation", "consultations"],
  ["consult", "consultations"],
  ["cartelle", "folders"],
  ["cartella", "folders"],
  ["folders", "folders"],
  ["folder", "folders"],
  ["fold", "folders"],
  ["cart", "folders"],
];

// Up to N suggestions per fetch keeps the dropdown manageable.
const SUGGESTION_LIMIT = 8;

interface EvidenceMentionOptions {
  /**
   * Patient whose fascicolo seeds the search. ``null`` disables the
   * suggestion plugin entirely (the user can still type the DSL by
   * hand). The hook is stored on the extension via ``configure`` so
   * the trigger has access to it from inside the suggestion plugin.
   */
  patientId: string | null;
}

export const EvidenceMentionExt = Extension.create<EvidenceMentionOptions>({
  name: "evidenceMention",

  addOptions() {
    return { patientId: null };
  },

  addProseMirrorPlugins() {
    const { patientId } = this.options;
    if (!patientId) return [];
    const suggestion = buildSuggestion(patientId, this.editor);
    return [Suggestion(suggestion)];
  },
});

// ---- suggestion plumbing ---------------------------------------------------

function buildSuggestion(
  patientId: string,
  editor: Editor,
): Omit<SuggestionOptions<EvidenceSuggestionItem>, "editor"> & {
  editor: Editor;
} {
  return {
    char: "@",
    allowSpaces: false,
    startOfLine: false,
    editor,

    // Suppress the trigger when ``@`` is preceded by a word character
    // (letters, digits, dot). This is what stops ``user@example.com``
    // from opening the dropdown halfway through an email address; it
    // also leaves "Vedi @study:..." working because the space before
    // ``@`` is a non-word character.
    allow: ({ state, range }) => {
      const $pos = state.doc.resolve(range.from);
      const offset = $pos.parentOffset;
      if (offset <= 1) return true; // start of paragraph (only the @)
      const before = $pos.parent.textBetween(offset - 2, offset - 1);
      // Whitespace, punctuation, line start → trigger.
      // Word character (letter/digit/_) → no trigger (likely an email).
      return !/[\w]/.test(before);
    },

    items: async ({ query }) => {
      // The query starts AFTER the ``@``. We branch in three steps:
      //
      // 1. ``tag:`` / ``tag`` → tag autocomplete (separate index).
      // 2. Kind word at the start (``stu``, ``studio``, ``doc``, ...)
      //    → scope the resource search to that section. The text
      //    after the kind word, if any, becomes the ILIKE prefix:
      //    ``@studio chest`` → studies whose title starts with
      //    "chest"; bare ``@studio`` → all studies (recent first).
      // 3. Otherwise → search every section with the typed text.
      const trimmed = query.trim();
      if (trimmed.startsWith("tag:") || trimmed === "tag") {
        const value = trimmed.replace(/^tag:?/, "").trim();
        return await fetchTagSuggestions(patientId, value);
      }
      const lower = trimmed.toLowerCase();
      for (const [prefix, section] of KIND_PREFIXES) {
        if (lower === prefix || lower.startsWith(`${prefix} `) || lower.startsWith(`${prefix}:`)) {
          const rest = trimmed
            .slice(prefix.length)
            .replace(/^[\s:]+/, "")
            .trim();
          return await fetchResourceSuggestions(patientId, rest, section);
        }
      }
      return await fetchResourceSuggestions(patientId, trimmed);
    },

    command: ({ editor: ed, range, props }) => {
      // Insert the resolved title as a markdown link whose href is
      // our DSL token (``@kind:UUID`` or ``@tag:value``). TipTap's
      // Markdown extension serialises this as ``[Title](@kind:UUID)``,
      // which the read-side parser recognises and renders as a
      // clickable pill displaying the title — same UX as a normal
      // markdown link, but the href is patient-scoped instead of
      // an http URL.
      const href = formatMentionHref(props);
      ed.chain()
        .focus()
        .insertContentAt(range, [
          {
            type: "text",
            text: props.label,
            marks: [{ type: "link", attrs: { href } }],
          },
          { type: "text", text: " " },
        ])
        .run();
    },

    render: () => {
      let component: ReactRenderer<SuggestionListHandle> | null = null;
      let popup: HTMLDivElement | null = null;

      function placePopup(rect: DOMRect | null | undefined) {
        if (!popup) return;
        const r = rect ?? null;
        if (r) {
          popup.style.left = `${r.left + window.scrollX}px`;
          popup.style.top = `${r.bottom + window.scrollY + 4}px`;
        }
      }

      return {
        onStart: (props) => {
          component = new ReactRenderer(SuggestionList, {
            props,
            editor: props.editor,
          });
          popup = document.createElement("div");
          popup.style.position = "absolute";
          popup.style.zIndex = "1100";
          document.body.appendChild(popup);
          popup.appendChild(component.element);
          placePopup(props.clientRect?.());
        },
        onUpdate: (props) => {
          component?.updateProps(props);
          placePopup(props.clientRect?.());
        },
        onKeyDown: (props) => {
          if (props.event.key === "Escape") {
            popup?.remove();
            component?.destroy();
            popup = null;
            component = null;
            return true;
          }
          return component?.ref?.onKeyDown?.(props.event) ?? false;
        },
        onExit: () => {
          popup?.remove();
          component?.destroy();
          popup = null;
          component = null;
        },
      };
    },
  };
}

function formatMentionHref(item: EvidenceSuggestionItem): string {
  if (item.kind === "tag") return `@tag:${item.id}`;
  return `@${item.kind}:${item.id}`;
}

async function fetchResourceSuggestions(
  patientId: string,
  query: string,
  section?: PatientSearchSection,
): Promise<EvidenceSuggestionItem[]> {
  // ``mentionSearch`` does ILIKE prefix matching on title fields and
  // accepts an empty ``q`` (returns recent items). Distinct from the
  // generic ``run`` which uses ``plainto_tsquery`` and only matches
  // whole words — a bare ``@`` or ``@stu`` would otherwise return
  // nothing. ``section`` scopes the search to one kind so e.g.
  // ``@studio`` returns every study (no text filter), not "studies
  // whose title starts with the word studio".
  try {
    const res = await patientSearchApi.mentionSearch(patientId, {
      q: query || undefined,
      sections: section,
      limit: SUGGESTION_LIMIT,
    });
    const items: EvidenceSuggestionItem[] = res.items.map((row) => ({
      kind: KIND_FROM_SECTION[row.section] ?? "study",
      id: row.id,
      label: row.title,
      hint: row.preview ?? null,
    }));
    items.sort(
      (a, b) =>
        KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.label.localeCompare(b.label),
    );
    return items;
  } catch {
    return [];
  }
}

async function fetchTagSuggestions(
  _patientId: string,
  prefix: string,
): Promise<EvidenceSuggestionItem[]> {
  // Tags are a global namespace today (``Tag`` table is keyed by
  // ``(target_kind, target_id)``, not patient). We pull from the
  // generic autocomplete endpoint and filter by the prefix the user
  // typed; cross-patient leak is irrelevant because tag *values* are
  // public signal — only resource ids are sensitive.
  try {
    const params = new URLSearchParams({ limit: String(SUGGESTION_LIMIT) });
    if (prefix.length > 0) params.set("q", prefix);
    const resp = await fetch(`/api/tags?${params.toString()}`);
    if (!resp.ok) return [];
    const rows: Array<{ namespace: string; value: string; count: number }> = await resp.json();
    return rows.map((row) => ({
      kind: "tag" as const,
      id: row.value,
      label: `#${row.value}`,
      hint: row.namespace !== "user" ? row.namespace : null,
    }));
  } catch {
    return [];
  }
}

// ---- dropdown render -------------------------------------------------------

import { useTranslations } from "next-intl";

interface SuggestionListProps {
  items: EvidenceSuggestionItem[];
  command: (item: EvidenceSuggestionItem) => void;
  /** Editor + range come straight from the suggestion plugin's
   *  render props; we use them to swap the current ``@<query>`` for
   *  ``@<kindWord>`` when the user clicks a kind chip in the
   *  header, so the dropdown re-filters without the user having to
   *  guess the right shorthand. */
  editor: Editor;
  range: { from: number; to: number };
  /** Raw text the user has typed AFTER ``@``. Used to decide whether
   *  to render the discoverability chips (only when query is short
   *  / empty — once the user is typing a search, chips are noise). */
  query: string;
}

interface SuggestionListHandle {
  onKeyDown: (event: KeyboardEvent) => boolean;
}

// Each chip shows a localised label and applies a kind prefix the
// items() router already understands (``studio`` / ``referto`` /
// ``doc`` / ``consult`` / ``tag:``). Chosen for natural Italian
// readability since the editor is patient-facing in IT first.
const FILTER_CHIPS: ReadonlyArray<{
  /** key used for the i18n label and the React key. */
  k: "studies" | "reports" | "documents" | "consultations" | "tag";
  prefix: string;
}> = [
  { k: "studies", prefix: "studio" },
  { k: "reports", prefix: "referto" },
  { k: "documents", prefix: "documento" },
  { k: "consultations", prefix: "consulto" },
  { k: "tag", prefix: "tag:" },
];

const SuggestionList = forwardRef(
  (props: SuggestionListProps, ref: ForwardedRef<SuggestionListHandle>) => {
    const tS = useTranslations("evidence.suggestion");
    const [selected, setSelected] = useState(0);

    // Reset selection on each fresh items array.
    // biome-ignore lint/correctness/useExhaustiveDependencies: ``props.items`` reference is the trigger — resetting on every render would cancel the user's selection.
    useEffect(() => {
      setSelected(0);
    }, [props.items]);

    const grouped = useMemo(() => groupByKind(props.items), [props.items]);

    useImperativeHandle(ref, () => ({
      onKeyDown: (event: KeyboardEvent) => {
        if (event.key === "ArrowDown") {
          setSelected((s) => (s + 1) % Math.max(1, props.items.length));
          return true;
        }
        if (event.key === "ArrowUp") {
          setSelected(
            (s) => (s - 1 + Math.max(1, props.items.length)) % Math.max(1, props.items.length),
          );
          return true;
        }
        if (event.key === "Enter") {
          const item = props.items[selected];
          if (item) {
            props.command(item);
            return true;
          }
        }
        return false;
      },
    }));

    function applyFilter(prefix: string) {
      // Replace ``@<currentQuery>`` with ``@<prefix>``. Keeping the
      // ``@`` re-arms the suggestion plugin's match finder; the
      // items() callback then runs again with the new query, which
      // hits the kind-prefix branch and scopes the search.
      props.editor.chain().focus().deleteRange(props.range).insertContent(`@${prefix}`).run();
    }

    function FilterChips() {
      // Hide chips once the user is past the kind-discovery phase:
      // they've typed something that's not just a kind prefix, so
      // showing the chips would cover the actual results.
      if (props.query.trim().length > 4) return null;
      return (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            gap: 4,
            padding: "6px 8px",
            borderBottom: "1px solid var(--bv-divider, #eef0f3)",
            background: "var(--bv-card-bg)",
          }}
        >
          <span
            style={{
              fontSize: "0.7rem",
              textTransform: "uppercase",
              letterSpacing: "0.04em",
              color: "var(--bv-muted)",
              marginRight: 4,
            }}
          >
            {tS("filterHeader")}
          </span>
          {FILTER_CHIPS.map((c) => (
            <button
              key={c.k}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => applyFilter(c.prefix)}
              style={{
                background: "var(--bv-info-soft)",
                color: "var(--bv-info)",
                border: "1px solid var(--bv-card-border)",
                borderRadius: 999,
                padding: "1px 8px",
                fontSize: "0.74rem",
                cursor: "pointer",
                fontWeight: 500,
              }}
            >
              {tS(`filter${capitalize(c.k)}` as const)}
            </button>
          ))}
        </div>
      );
    }

    if (props.items.length === 0) {
      return (
        <div
          style={{
            background: "var(--bv-card-bg)",
            border: "1px solid var(--bv-card-border)",
            borderRadius: 6,
            boxShadow: "var(--bv-shadow-2)",
            fontSize: "0.82rem",
            color: "var(--bv-muted)",
            minWidth: 280,
            maxWidth: 380,
          }}
        >
          <FilterChips />
          <div style={{ padding: "8px 10px" }}>{tS("emptyHint")}</div>
        </div>
      );
    }

    return (
      <div
        style={{
          background: "var(--bv-card-bg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: 6,
          boxShadow: "var(--bv-shadow-2)",
          minWidth: 280,
          maxWidth: 380,
          maxHeight: 360,
          overflowY: "auto",
          fontSize: "0.85rem",
          color: "var(--bv-fg)",
        }}
      >
        <FilterChips />
        {grouped.map(([kind, items]) => (
          <div key={kind}>
            <div
              style={{
                fontSize: "0.7rem",
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                padding: "6px 10px 2px",
                color: "var(--bv-muted)",
              }}
            >
              {kind}
            </div>
            {items.map((it) => {
              const idx = props.items.indexOf(it);
              const isSel = idx === selected;
              return (
                <button
                  key={`${kind}:${it.id}`}
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => props.command(it)}
                  onMouseEnter={() => setSelected(idx)}
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    width: "100%",
                    textAlign: "left",
                    padding: "6px 10px",
                    background: isSel ? "var(--bv-divider)" : "transparent",
                    color: "var(--bv-fg)",
                    border: "none",
                    cursor: "pointer",
                    fontSize: "0.85rem",
                  }}
                >
                  <span style={{ fontWeight: 500 }}>{it.label}</span>
                  {it.hint && (
                    <span
                      style={{
                        fontSize: "0.72rem",
                        color: "var(--bv-muted)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {it.hint}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        ))}
      </div>
    );
  },
);

function capitalize(s: string): "Studies" | "Reports" | "Documents" | "Consultations" | "Tag" {
  return (s.charAt(0).toUpperCase() + s.slice(1)) as
    | "Studies"
    | "Reports"
    | "Documents"
    | "Consultations"
    | "Tag";
}

SuggestionList.displayName = "EvidenceSuggestionList";

function groupByKind(
  items: EvidenceSuggestionItem[],
): Array<[EvidenceSuggestionKind, EvidenceSuggestionItem[]]> {
  const map = new Map<EvidenceSuggestionKind, EvidenceSuggestionItem[]>();
  for (const it of items) {
    const arr = map.get(it.kind) ?? [];
    arr.push(it);
    map.set(it.kind, arr);
  }
  const out: Array<[EvidenceSuggestionKind, EvidenceSuggestionItem[]]> = [];
  for (const k of KIND_ORDER) {
    const arr = map.get(k);
    if (arr && arr.length > 0) out.push([k, arr]);
  }
  return out;
}
