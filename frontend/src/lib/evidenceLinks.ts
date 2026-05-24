// Evidenze e sintesi inline link DSL — TS counterpart of the backend
// parser in ``services/evidence_links.py``. Recognises:
//
//   @study:UID         pointer to a DICOM study
//   @series:UID        pointer to a series
//   @folder:UID        pointer to a folder
//   @document:UID      pointer to a patient document
//   @consultation:UID  pointer to a consultation
//   @report:UID        pointer to a report
//   @tag:value         clickable tag chip (was ``#tag``; ``#`` collides
//                      with markdown headings, so the trigger is now
//                      unified under ``@``)
//
// The parser is a pure regex pass and is shared between the read-side
// renderer (markdown → React clickable pills) and the write-side
// editor (live highlight + cross-patient validation feedback).
//
// Cross-patient validation lives on the server: any save that mentions
// a UUID from another patient is rejected with HTTP 422 carrying the
// list of offending raw spans (see ``EvidenceLinkViolation`` below).

export type EvidenceMentionKind =
  | "study"
  | "series"
  | "folder"
  | "document"
  | "consultation"
  | "report";

export type EvidenceTagKind = "tag";

export type EvidenceTokenKind = EvidenceMentionKind | EvidenceTagKind;

export interface EvidenceMention {
  kind: EvidenceMentionKind;
  raw: string;
  /** UUID of the referenced resource. */
  targetId: string;
  /** Character offset in the source string where ``raw`` starts. */
  start: number;
  /** Exclusive end offset. */
  end: number;
  /**
   * Human title from the markdown-link form ``[Title](@kind:UUID)``.
   * Undefined for the bare form ``@kind:UUID``; the renderer falls
   * back to a default label in that case.
   */
  title?: string;
}

export interface EvidenceTag {
  kind: EvidenceTagKind;
  raw: string;
  value: string;
  start: number;
  end: number;
  /** Title from ``[Title](@tag:value)`` form, undefined for bare. */
  title?: string;
}

export type EvidenceToken = EvidenceMention | EvidenceTag;

export interface EvidencePlainSegment {
  kind: "text";
  text: string;
  start: number;
  end: number;
}

export type EvidenceSegment = EvidencePlainSegment | EvidenceToken;

// Two surface forms — same as the backend ``_LINK_MENTION_RE`` /
// ``_BARE_MENTION_RE``. The link form is what the editor's
// autocomplete writes so the user sees a human title in both
// WYSIWYG and raw-markdown modes; the bare form is kept for
// backwards-compat with hand-typed / migrated content.
const _UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
// Kind alternatives, longest-first so ``studies`` wins over ``study``
// under the leftmost-longest semantics of the regex engine. The
// plural forms are normalised to the canonical singular by
// ``normaliseKind`` after match — see backend
// ``_KIND_ALIASES`` for the symmetric mapping.
const _KIND =
  "studies|study|series|folders|folder|documents|document|" +
  "consultations|consultation|reports|report";
const LINK_MENTION_RE = new RegExp(`\\[([^\\]\\n]+)\\]\\(@(${_KIND}):(${_UUID})\\)`, "g");
const BARE_MENTION_RE = new RegExp(`@(${_KIND}):(${_UUID})`, "g");

const _KIND_ALIASES: Readonly<Record<string, EvidenceMentionKind>> = {
  studies: "study",
  study: "study",
  series: "series",
  folders: "folder",
  folder: "folder",
  documents: "document",
  document: "document",
  consultations: "consultation",
  consultation: "consultation",
  reports: "report",
  report: "report",
};

function normaliseKind(raw: string): EvidenceMentionKind | null {
  return _KIND_ALIASES[raw] ?? null;
}

// Tags use ``@tag:value`` (was ``#value``) to avoid the markdown
// heading collision: a line starting with ``# `` is an H1, not a tag.
// Same character set + length cap as the backend ``_BARE_TAG_RE``.
const _TAG_VALUE = "[A-Za-z0-9][A-Za-z0-9._/\\-]{0,63}";
const LINK_TAG_RE = new RegExp(`\\[([^\\]\\n]+)\\]\\(@tag:(${_TAG_VALUE})\\)`, "g");
const BARE_TAG_RE = new RegExp(`@tag:(${_TAG_VALUE})`, "g");

const MENTION_KINDS: ReadonlyArray<EvidenceMentionKind> = [
  "study",
  "series",
  "folder",
  "document",
  "consultation",
  "report",
];

export function isMentionKind(s: string): s is EvidenceMentionKind {
  return (MENTION_KINDS as readonly string[]).includes(s);
}

/**
 * Walk the body and return every recognised token (mentions + tags)
 * sorted by start offset. Two passes per kind: link form first, bare
 * form second — the bare scan skips spans already matched by the
 * link form so the same UUID isn't double-counted. Pure regex; no IO.
 */
export function parseEvidenceTokens(body: string): EvidenceToken[] {
  const out: EvidenceToken[] = [];
  const covered: Array<[number, number]> = [];

  for (const m of body.matchAll(LINK_MENTION_RE)) {
    if (m.index === undefined) continue;
    const kind = normaliseKind(m[2]);
    if (kind === null) continue;
    const start = m.index;
    const end = start + m[0].length;
    out.push({ kind, raw: m[0], title: m[1], targetId: m[3], start, end });
    covered.push([start, end]);
  }
  for (const m of body.matchAll(LINK_TAG_RE)) {
    if (m.index === undefined) continue;
    const start = m.index;
    const end = start + m[0].length;
    out.push({
      kind: "tag",
      raw: m[0],
      title: m[1],
      value: m[2],
      start,
      end,
    });
    covered.push([start, end]);
  }

  const inside = (pos: number): boolean => covered.some(([s, e]) => s <= pos && pos < e);

  for (const m of body.matchAll(BARE_MENTION_RE)) {
    if (m.index === undefined || inside(m.index)) continue;
    const kind = normaliseKind(m[1]);
    if (kind === null) continue;
    out.push({
      kind,
      raw: m[0],
      targetId: m[2],
      start: m.index,
      end: m.index + m[0].length,
    });
  }
  for (const m of body.matchAll(BARE_TAG_RE)) {
    if (m.index === undefined || inside(m.index)) continue;
    out.push({
      kind: "tag",
      raw: m[0],
      value: m[1],
      start: m.index,
      end: m.index + m[0].length,
    });
  }

  out.sort((a, b) => a.start - b.start);
  return out;
}

/**
 * Split the body into an ordered list of plain-text segments and
 * recognised tokens. Useful for the read-side renderer that wants
 * to walk the body once and emit React nodes in source order.
 */
export function segmentEvidenceBody(body: string): EvidenceSegment[] {
  const tokens = parseEvidenceTokens(body);
  if (tokens.length === 0) {
    return body.length === 0 ? [] : [{ kind: "text", text: body, start: 0, end: body.length }];
  }
  const segs: EvidenceSegment[] = [];
  let cursor = 0;
  for (const t of tokens) {
    if (t.start > cursor) {
      segs.push({
        kind: "text",
        text: body.slice(cursor, t.start),
        start: cursor,
        end: t.start,
      });
    }
    segs.push(t);
    cursor = t.end;
  }
  if (cursor < body.length) {
    segs.push({
      kind: "text",
      text: body.slice(cursor),
      start: cursor,
      end: body.length,
    });
  }
  return segs;
}

// Match the bare DSL form anchored to start/end so we can detect when
// ReactMarkdown has handed us a parsed ``<a href="@kind:UUID">`` and
// route it to the MentionPill instead of letting the raw href leak
// through to the browser as an unresolvable relative reference.
const DSL_MENTION_HREF_RE = new RegExp(`^@(${_KIND}):(${_UUID})$`);
const DSL_TAG_HREF_RE = new RegExp(`^@tag:(${_TAG_VALUE})$`);

export interface ParsedMentionHref {
  kind: EvidenceMentionKind;
  targetId: string;
}

/**
 * If ``href`` is a DSL mention pointer (``@kind:UUID``), return the
 * normalised kind + target id. Returns ``null`` for non-DSL hrefs
 * (regular external URLs, anchors, ``mailto:``, ...). Plural forms
 * (``@documents:UUID``) are accepted and normalised to singular.
 */
export function parseMentionHref(href: string): ParsedMentionHref | null {
  const m = DSL_MENTION_HREF_RE.exec(href);
  if (!m) return null;
  const kind = normaliseKind(m[1]);
  if (kind === null) return null;
  return { kind, targetId: m[2] };
}

/**
 * If ``href`` is a DSL tag pointer (``@tag:value``), return the
 * tag value. Returns ``null`` otherwise.
 */
export function parseTagHref(href: string): string | null {
  const m = DSL_TAG_HREF_RE.exec(href);
  return m ? m[1] : null;
}

/**
 * Server-side validation error shape returned by the backend at HTTP
 * 422 when a save introduces a cross-patient or unresolvable mention.
 * Mirrors ``MentionViolation`` in ``services/evidence_links.py``.
 */
export interface EvidenceLinkViolation {
  raw: string;
  kind: EvidenceMentionKind;
  reason: "not_found" | "cross_patient";
}

export interface EvidenceLinkErrorDetail {
  code: "cross_patient_or_missing_link";
  violations: EvidenceLinkViolation[];
}

/**
 * Best-effort parser for the 422 ``detail`` payload. Returns ``null``
 * when the response shape doesn't match (e.g. the error is a generic
 * Pydantic validation array). Callers should fall back to a generic
 * error message in that case.
 *
 * The Problem Details middleware spreads the backend ``detail`` dict
 * into the response body and *also* fills ``detail`` with the localised
 * status title (``"Validation failed"``). The parser therefore probes
 * both levels: callers can pass either ``ApiError.detail`` (the full
 * body, where ``code`` lives at top level after the spread) or the
 * legacy nested ``detail.detail`` (a string, which we ignore).
 */
function _readError(value: unknown): EvidenceLinkErrorDetail | null {
  if (
    value == null ||
    typeof value !== "object" ||
    !("code" in value) ||
    (value as { code: unknown }).code !== "cross_patient_or_missing_link"
  ) {
    return null;
  }
  const v = (value as { violations?: unknown }).violations;
  if (!Array.isArray(v)) return null;
  const violations: EvidenceLinkViolation[] = [];
  for (const row of v) {
    if (
      row &&
      typeof row === "object" &&
      typeof (row as { raw: unknown }).raw === "string" &&
      typeof (row as { kind: unknown }).kind === "string" &&
      isMentionKind((row as { kind: string }).kind) &&
      ((row as { reason: unknown }).reason === "not_found" ||
        (row as { reason: unknown }).reason === "cross_patient")
    ) {
      violations.push({
        raw: (row as { raw: string }).raw,
        kind: (row as { kind: EvidenceMentionKind }).kind,
        reason: (row as { reason: "not_found" | "cross_patient" }).reason,
      });
    }
  }
  return { code: "cross_patient_or_missing_link", violations };
}

export function parseEvidenceLinkError(detail: unknown): EvidenceLinkErrorDetail | null {
  // Try the body as-is first (post-Problem-Details middleware shape:
  // ``code`` lives at top level alongside ``type``, ``title``,
  // ``status``, ``detail``, ``instance``, ``violations``). If that
  // doesn't match, walk one level down to support call sites that
  // already extract ``detail.detail`` (a defensive habit older code
  // adopted before the middleware shape was stable).
  return _readError(detail) ?? _readError((detail as { detail?: unknown })?.detail);
}
