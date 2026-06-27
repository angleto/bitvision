"use client";

// Read-side renderer for an Evidenze e sintesi note. Parses Markdown
// structure (paragraphs, headers, bold/italic, lists, links) via
// ReactMarkdown, then walks every text node to splice in our DSL
// segments (``@kind:UUID`` mentions, ``#tag`` tags) as clickable
// pills.
//
// The body is markdown by construction: ``EvidenceEditor`` writes
// markdown out via ``tiptap-markdown``, so the read side must mirror
// that structure or the user sees raw ``**`` / ``##`` / ``-`` instead
// of formatted text.
//
// Cross-link routes (all patient-namespaced — cross-patient hop is
// structurally inexpressible from a mention):
//
//   @study:UID         -> /patients/{pid}/studies/{UID}?ctx=evidence:{pid}
//   @series:UID        -> /patients/{pid}/studies/{UID} (series rolls up to its study)
//   @folder:UID        -> /patients/{pid}?folder={UID}&ctx=evidence:{pid}
//                          (the fascicolo resolves the uuid to a path on mount)
//   @document:UID      -> /patients/{pid}/documents/{UID}?ctx=evidence:{pid}
//   @consultation:UID  -> /patients/{pid}/consultations/{UID}?ctx=evidence:{pid}
//   @report:UID        -> /patients/{pid}/studies/{study-id-of-UID}?reportId=UID
//   #tag               -> /patients/{pid}/tags/{value}
//
// Cross-patient guarantees: the ``patientId`` prop is the *active*
// patient — the same one whose body we're rendering. The destination
// URL always carries the same patient id; the backend already
// rejected any save with a cross-patient mention so we don't even
// have to worry about defending the link target here.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { Fragment, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";

import {
  type EvidenceMention,
  type EvidenceMentionKind,
  type EvidenceTag,
  isMentionKind,
  parseMentionHref,
  parseTagHref,
  segmentEvidenceBody,
} from "@/lib/evidenceLinks";

// react-markdown ships a default URL sanitiser that strips any href
// whose scheme isn't in the http/https/mailto/xmpp/irc whitelist.
// Our DSL hrefs ``@kind:UUID`` and ``@tag:value`` look like a custom
// scheme to that sanitiser (``@document`` before the colon is an
// invalid scheme name), so the href arrives at the custom ``a:``
// component as an empty string and the click silently navigates to
// the page root. This wrapper lets DSL hrefs through unchanged and
// defers everything else to the default sanitiser, keeping the
// XSS-safety guarantee for regular links.
function urlTransform(href: string): string {
  if (href.startsWith("@")) return href;
  return defaultUrlTransform(href);
}

interface Props {
  patientId: string;
  body: string;
  /**
   * Identifier of the source note (or arbitrary token) added to the
   * ``ctx`` query param so the destination page can render a
   * "Torna a Evidenze e sintesi" chip and navigate back. Pass
   * ``null`` to suppress the ctx propagation.
   */
  ctx?: string | null;
}

export default function EvidenceContent({ patientId, body, ctx }: Props) {
  if (!body || !body.trim()) {
    return null;
  }
  const ctxToken = ctx ?? `evidence:${patientId}`;
  const proc = (children: ReactNode) =>
    walkChildren(children, (text, k) => renderSegments(text, patientId, ctxToken, k));
  return (
    <div
      style={{
        lineHeight: 1.55,
        // The read view must NEVER overflow its container, whatever the
        // body holds. Without this, an expanded note (the sticky drops
        // ``overflow:hidden`` to ``visible`` when expanded) spills long
        // URLs / unbroken tokens / code blocks / long mention pills off
        // the side of the page and becomes unreadable — worst on mobile
        // where the box is ~343px wide. ``overflowWrap: anywhere`` breaks
        // long strings; ``minWidth: 0`` lets the box shrink inside any
        // flex/grid ancestor. ``maxWidth: min(72ch, 100%)`` also caps the
        // reading measure on wide desktops without affecting narrow ones.
        maxWidth: "min(72ch, 100%)",
        minWidth: 0,
        overflowWrap: "anywhere",
        wordBreak: "break-word",
      }}
    >
      <ReactMarkdown
        urlTransform={urlTransform}
        components={{
          p: ({ children }) => <p style={{ margin: "0.4rem 0" }}>{proc(children)}</p>,
          h1: ({ children }) => <h3 style={{ margin: "0.6rem 0 0.3rem" }}>{proc(children)}</h3>,
          h2: ({ children }) => <h4 style={{ margin: "0.6rem 0 0.3rem" }}>{proc(children)}</h4>,
          h3: ({ children }) => <h5 style={{ margin: "0.5rem 0 0.3rem" }}>{proc(children)}</h5>,
          h4: ({ children }) => <h6 style={{ margin: "0.5rem 0 0.3rem" }}>{proc(children)}</h6>,
          h5: ({ children }) => <h6 style={{ margin: "0.5rem 0 0.3rem" }}>{proc(children)}</h6>,
          h6: ({ children }) => <h6 style={{ margin: "0.5rem 0 0.3rem" }}>{proc(children)}</h6>,
          // Inline/pasted images must scale down to the card, never
          // force the note wider than its container.
          img: ({ src, alt }) => (
            <img
              src={typeof src === "string" ? src : undefined}
              alt={alt ?? ""}
              style={{ maxWidth: "100%", height: "auto", borderRadius: 4 }}
            />
          ),
          ul: ({ children }) => (
            <ul style={{ margin: "0.3rem 0", paddingLeft: "1.4em" }}>{children}</ul>
          ),
          ol: ({ children }) => (
            <ol style={{ margin: "0.3rem 0", paddingLeft: "1.4em" }}>{children}</ol>
          ),
          li: ({ children }) => <li>{proc(children)}</li>,
          em: ({ children }) => <em>{proc(children)}</em>,
          strong: ({ children }) => <strong>{proc(children)}</strong>,
          a: ({ href, children }) => {
            // ReactMarkdown turns ``[Title](@kind:UUID)`` into a plain
            // anchor before our text-walker ever sees it, so the
            // raw DSL href would otherwise leak through as a broken
            // relative URL. Intercept here and re-route to the same
            // pill components used for the bare form. Title comes
            // from the link's children — a string or a ReactNode tree
            // we flatten to text for the pill label.
            if (href) {
              const mention = parseMentionHref(href);
              if (mention) {
                const titleText = flattenToText(children);
                return (
                  <MentionPill
                    patientId={patientId}
                    ctxToken={ctxToken}
                    mention={{
                      kind: mention.kind,
                      raw: `[${titleText}](${href})`,
                      targetId: mention.targetId,
                      title: titleText,
                      start: 0,
                      end: 0,
                    }}
                  />
                );
              }
              const tagValue = parseTagHref(href);
              if (tagValue) {
                const titleText = flattenToText(children);
                return (
                  <TagPill
                    patientId={patientId}
                    ctxToken={ctxToken}
                    tag={{
                      kind: "tag",
                      raw: `[${titleText}](${href})`,
                      value: tagValue,
                      title: titleText,
                      start: 0,
                      end: 0,
                    }}
                  />
                );
              }
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
          // Fallback wrapper so blockquote / code / table cells still
          // segment text properly even if not explicitly listed.
          blockquote: ({ children }) => (
            <blockquote
              style={{
                borderLeft: "3px solid var(--bv-card-border, #e5e7eb)",
                paddingLeft: "0.6em",
                margin: "0.4rem 0",
                color: "var(--bv-fg-soft, #555)",
              }}
            >
              {children}
            </blockquote>
          ),
          code: ({ children }) => (
            <code
              style={{
                background: "var(--bv-card-bg, #f6f8fa)",
                padding: "0.05em 0.35em",
                borderRadius: 3,
                fontSize: "0.92em",
                overflowWrap: "anywhere",
              }}
            >
              {children}
            </code>
          ),
          // Fenced code blocks render as <pre><code>. Without an explicit
          // override <pre> is ``white-space: pre`` (no wrapping), so a
          // single long line escapes the card and breaks the whole page.
          // Wrap it instead of clipping so the note stays readable.
          pre: ({ children }) => (
            <pre
              style={{
                margin: "0.4rem 0",
                padding: "0.5rem 0.6rem",
                background: "var(--bv-card-bg, #f6f8fa)",
                borderRadius: 6,
                fontSize: "0.85rem",
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                wordBreak: "break-word",
                maxWidth: "100%",
              }}
            >
              {children}
            </pre>
          ),
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}

// Walk a ReactMarkdown ``children`` payload (string | ReactElement |
// array thereof) and replace every plain string with the corresponding
// list of styled segments (text spans + Mention/Tag pills). Non-string
// nodes pass through untouched so ReactMarkdown's nested formatting
// (``<strong>``, ``<em>``, links, etc.) keeps working — when we recurse
// into those wrappers we catch their text children too.
// Reduce a ReactNode tree (string | ReactElement | array) to plain
// text. Used to recover the title of a markdown link after
// ReactMarkdown has already turned it into a JSX element. Anything
// non-textual (icons, images) reads as empty so the pill falls back
// to its kind+id label rather than swallowing visual content.
function flattenToText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(flattenToText).join("");
  if (node && typeof node === "object" && "props" in node) {
    const props = (node as { props?: { children?: ReactNode } }).props;
    return props ? flattenToText(props.children) : "";
  }
  return "";
}

function walkChildren(
  children: ReactNode,
  renderText: (text: string, key: string) => ReactNode,
): ReactNode {
  if (typeof children === "string") return renderText(children, "0");
  if (Array.isArray(children)) {
    return children.map((c, i) => {
      if (typeof c === "string") {
        // Index-based keys are safe here: markdown children come from
        // a single immutable parse, no reorder/insert mid-life.
        // biome-ignore lint/suspicious/noArrayIndexKey: stable order
        return <Fragment key={`s-${i}`}>{renderText(c, `${i}`)}</Fragment>;
      }
      return c;
    });
  }
  return children;
}

function renderSegments(
  text: string,
  patientId: string,
  ctxToken: string,
  keyPrefix: string,
): ReactNode[] {
  const segments = segmentEvidenceBody(text);
  const out: ReactNode[] = [];
  let i = 0;
  for (const seg of segments) {
    const k = `${keyPrefix}-${i++}`;
    if (seg.kind === "text") {
      out.push(<Fragment key={k}>{seg.text}</Fragment>);
      continue;
    }
    if (seg.kind === "tag") {
      out.push(<TagPill key={k} patientId={patientId} tag={seg} ctxToken={ctxToken} />);
      continue;
    }
    out.push(<MentionPill key={k} patientId={patientId} mention={seg} ctxToken={ctxToken} />);
  }
  return out;
}

// Per-kind visual style. Four colour families across seven kinds so a
// reader can sort the mentions at a glance instead of having to read
// every label:
//
//   blue   → imaging axis    (study, series)
//   green  → documentary axis (document, report)
//   amber  → workflow axis    (consultation)
//   yellow → taxonomy axis    (tag, handled by TagPill)
//   neutral→ container axis   (folder)
//
// Within a family, kinds that collide on colour are disambiguated by
// the leading glyph + a 1px border in the same hue: ``S`` vs ``Sr``,
// ``D`` vs ``R``. Glyphs are deliberately Latin/typographic, not emoji,
// so they survive print export and screen reader pronunciation without
// surprises.
const KIND_STYLE: Record<
  EvidenceMentionKind,
  { bg: string; fg: string; border: string; glyph: string }
> = {
  study: {
    bg: "var(--bv-info-soft, #eef4ff)",
    fg: "var(--bv-info, #1e40af)",
    border: "transparent",
    glyph: "S",
  },
  series: {
    bg: "var(--bv-info-soft, #eef4ff)",
    fg: "var(--bv-info, #1e40af)",
    border: "var(--bv-info, #1e40af)",
    glyph: "Sr",
  },
  document: {
    bg: "var(--bv-success-soft, #ecfdf5)",
    fg: "var(--bv-success, #047857)",
    border: "transparent",
    glyph: "D",
  },
  report: {
    bg: "var(--bv-success-soft, #ecfdf5)",
    fg: "var(--bv-success, #047857)",
    border: "var(--bv-success, #047857)",
    glyph: "R",
  },
  consultation: {
    bg: "var(--bv-accent-soft, #fff3e8)",
    fg: "var(--bv-accent, #e96b1f)",
    border: "transparent",
    glyph: "C",
  },
  folder: {
    bg: "var(--bv-card-bg, #f6f8fa)",
    fg: "var(--bv-fg-soft, #334155)",
    border: "var(--bv-card-border, #d0d5dd)",
    glyph: "▸", // ▸
  },
};

function MentionPill({
  patientId,
  mention,
  ctxToken,
}: {
  patientId: string;
  mention: EvidenceMention;
  ctxToken: string;
}) {
  const t = useTranslations("evidence.mention");
  const href = mentionHref(patientId, mention.kind, mention.targetId, ctxToken);
  // ``t`` throws if the key is missing; the kinds in the DSL are a
  // closed set tied to the parser, so we know they all have entries.
  const kindLabel = isMentionKind(mention.kind) ? t(mention.kind) : capitalize(mention.kind);
  const style = KIND_STYLE[mention.kind] ?? KIND_STYLE.study;
  // Prefer the human title written by the editor (link-form mention,
  // ``[Title](@kind:UUID)``); fall back to the kind label + short id
  // when the body uses the bare form.
  return (
    <Link
      href={href}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        background: style.bg,
        color: style.fg,
        border: `1px solid ${style.border}`,
        borderRadius: 999,
        padding: "0px 8px 0px 6px",
        fontSize: "0.82rem",
        fontWeight: 500,
        textDecoration: "none",
        margin: "0 1px",
        whiteSpace: "nowrap",
        maxWidth: "100%",
        minWidth: 0,
        verticalAlign: "bottom",
      }}
      title={`${kindLabel}${mention.raw ? ` — ${mention.raw}` : ""}`}
      aria-label={`${kindLabel}: ${mention.title ?? shortId(mention.targetId)}`}
    >
      <span
        aria-hidden
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          minWidth: 18,
          height: 18,
          padding: "0 4px",
          borderRadius: 999,
          background: style.fg,
          color: style.bg,
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: "0.68rem",
          fontWeight: 700,
          letterSpacing: "0.02em",
          lineHeight: 1,
        }}
      >
        {style.glyph}
      </span>
      {mention.title ? (
        <span
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            minWidth: 0,
          }}
        >
          {mention.title}
        </span>
      ) : (
        <>
          <span style={{ fontWeight: 600 }}>{kindLabel}</span>
          <span
            style={{
              fontFamily: "ui-monospace, monospace",
              fontSize: "0.74rem",
              opacity: 0.8,
            }}
          >
            {shortId(mention.targetId)}
          </span>
        </>
      )}
    </Link>
  );
}

function TagPill({
  patientId,
  tag,
  ctxToken,
}: {
  patientId: string;
  tag: EvidenceTag;
  ctxToken: string;
}) {
  const params = new URLSearchParams({ ctx: ctxToken });
  const href = `/patients/${patientId}/tags/${encodeURIComponent(tag.value)}?${params.toString()}`;
  // Use the explicit title from the link form if present, otherwise
  // fall back to the canonical ``#value`` label.
  const label = tag.title ?? `#${tag.value}`;
  return (
    <Link
      href={href}
      style={{
        display: "inline-flex",
        alignItems: "center",
        background: "var(--bv-warning-soft, #fef3c7)",
        color: "var(--bv-warning, #b45309)",
        borderRadius: 999,
        padding: "1px 8px",
        fontSize: "0.82rem",
        fontWeight: 500,
        textDecoration: "none",
        margin: "0 1px",
        whiteSpace: "nowrap",
        maxWidth: "100%",
        minWidth: 0,
        verticalAlign: "bottom",
      }}
    >
      <span
        style={{
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          minWidth: 0,
        }}
      >
        {label}
      </span>
    </Link>
  );
}

function mentionHref(
  patientId: string,
  kind: EvidenceMentionKind,
  targetId: string,
  ctxToken: string,
): string {
  // Mentions reuse the legacy ``?from=notes&note=<id>`` query the
  // notes page seeded into study + document detail pages. That gets
  // us a "← Torna alle evidenze" back-link on those pages with no
  // extra wiring. The newer ``ctx=evidence:...`` token is added in
  // parallel for surfaces that opt into ``BackToEvidenceChip``
  // (e.g. tag aggregation pages).
  const noteId = ctxToken.startsWith("evidence:note:")
    ? ctxToken.slice("evidence:note:".length)
    : null;
  const fromQs = noteId ? `from=notes&note=${encodeURIComponent(noteId)}` : "from=notes";
  const ctx = encodeURIComponent(ctxToken);
  switch (kind) {
    case "study":
    case "series":
    case "report":
      // Patient-namespaced canonical form. Cross-patient mentions
      // are already rejected by the backend at save-time, so the
      // ``patientId`` we carry here is guaranteed to own the target.
      return `/patients/${patientId}/studies/${targetId}?${fromQs}&ctx=${ctx}`;
    case "document":
      return `/patients/${patientId}/documents/${targetId}?${fromQs}&ctx=${ctx}`;
    case "consultation":
      return `/patients/${patientId}/consultations/${targetId}?${fromQs}&ctx=${ctx}`;
    case "folder":
      return `/patients/${patientId}?folder=${targetId}&ctx=${ctx}`;
    default:
      return `/patients/${patientId}?ctx=${ctx}`;
  }
}

function capitalize(s: string): string {
  return s.length === 0 ? s : s[0].toUpperCase() + s.slice(1);
}

function shortId(uuid: string): string {
  return uuid.slice(0, 8);
}
