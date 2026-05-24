"use client";

// WYSIWYG editor for Evidenze e sintesi note bodies. TipTap with
// StarterKit + Link + Placeholder + tiptap-markdown so the saved
// payload stays plain markdown (round-trips through ``Markdown`` /
// ``EvidenceContent`` on the read side without a parser swap).
//
// Cross-patient guard: every save validates the body server-side
// (clinical_notes endpoint calls ``services.evidence_links``).
// HTTP 422 with structured ``violations`` is surfaced inline above
// the editor; the user must remove or replace the offending mentions
// before save can succeed.

import { Link as TiptapLink } from "@tiptap/extension-link";
import { Placeholder } from "@tiptap/extension-placeholder";
import { EditorContent, useEditor } from "@tiptap/react";
import { StarterKit } from "@tiptap/starter-kit";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
// `tiptap-markdown` ships as a single-file extension; the runtime
// API is identical between TipTap 2 and 3.
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore — no bundled types in tiptap-markdown 0.9.x
import { Markdown } from "tiptap-markdown";

import { EvidenceMentionExt } from "@/components/EvidenceMentionExtension";
import type { EvidenceLinkViolation } from "@/lib/evidenceLinks";

interface Props {
  value: string;
  onChange: (markdown: string) => void;
  /** Required unless ``embedded`` is true. */
  onSave?: () => void | Promise<void>;
  /** Required unless ``embedded`` is true. */
  onCancel?: () => void;
  busy?: boolean;
  saveLabel?: string;
  cancelLabel?: string;
  saveBusyLabel?: string;
  /** Tooltip on the save / cancel buttons. Used to surface keyboard
   *  shortcuts (Cmd+Enter, Esc) the parent owns at the document
   *  level. Optional — when omitted the buttons render without a
   *  ``title`` attribute. */
  saveTitle?: string;
  cancelTitle?: string;
  /**
   * When true, hides the save / cancel button row so the editor can
   * sit inside a parent form that owns the submit action. The editor
   * still emits ``onChange`` on every keystroke so the parent form
   * keeps its own controlled state up to date.
   */
  embedded?: boolean;
  /**
   * Server-side validation errors (cross-patient or missing target).
   * Rendered inline above the editor so the user can locate the
   * offending span(s).
   */
  errors?: EvidenceLinkViolation[];
  /**
   * Patient whose fascicolo seeds the ``@``-mention autocomplete. The
   * suggestion plugin queries ``/api/patients/{id}/search`` for resource
   * names and ``/api/tags`` for tag values. ``undefined`` disables
   * autocomplete; the user can still type ``@kind:UUID`` manually.
   */
  patientId?: string | null;
}

export default function EvidenceEditor({
  value,
  onChange,
  onSave,
  onCancel,
  busy,
  saveLabel,
  cancelLabel,
  saveBusyLabel,
  saveTitle,
  cancelTitle,
  embedded = false,
  errors,
  patientId,
}: Props) {
  const tErr = useTranslations("evidence.linkError");
  const tEd = useTranslations("evidence.editor");
  // ``raw`` lets the user drop down to a plain ``<textarea>`` when
  // they want full markdown control (paste long blocks, fix a bad
  // round-trip, etc.). Same payload model: both modes read from /
  // write to the same ``value`` markdown string. When switching
  // back to WYSIWYG we reload TipTap's content from the latest
  // markdown so any raw edits show through.
  const [rawMode, setRawMode] = useState(false);
  const editor = useEditor(
    {
      // The editor is mounted in the client; suppress the SSR mismatch
      // warning Next emits for first paint by keeping the initial
      // ProseMirror DOM stable.
      immediatelyRender: false,
      extensions: [
        StarterKit.configure({
          // We bring our own Link extension so we can disable
          // auto-link (it would clash with the @kind:UUID syntax).
          link: false,
        }),
        TiptapLink.configure({
          openOnClick: false,
          autolink: false,
          // Accept the Evidenze e sintesi DSL hrefs
          // (``@study:UUID``, ``@tag:value``, ...) in addition to
          // regular http(s) / mailto. Without this the Link mark
          // strips the href as invalid and the autocomplete output
          // collapses to plain text.
          validate: (url: string) => {
            if (!url) return false;
            if (/^@(?:study|series|folder|document|consultation|report|tag):/.test(url)) {
              return true;
            }
            return /^(https?:|mailto:|tel:)/i.test(url);
          },
          HTMLAttributes: {
            rel: "noopener noreferrer nofollow",
            target: "_blank",
          },
        }),
        Placeholder.configure({
          placeholder: tEd("placeholder"),
        }),
        Markdown.configure({
          html: false,
          transformPastedText: true,
          transformCopiedText: true,
        }),
        EvidenceMentionExt.configure({
          patientId: patientId ?? null,
        }),
      ],
      content: value,
      onUpdate: ({ editor }) => {
        const md = (
          editor.storage as unknown as {
            markdown?: { getMarkdown(): string };
          }
        ).markdown?.getMarkdown();
        if (typeof md === "string") onChange(md);
      },
    },
    // Re-create the editor instance only when the parent swaps the
    // note (initial value comes from props). In-flight edits live on
    // the editor itself, not in React state, so we don't react to
    // ``value`` changes here.
    [],
  );

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      editor?.destroy();
    };
  }, [editor]);

  // When the user flips ``raw → WYSIWYG`` we resync TipTap from the
  // current markdown so anything they typed in the textarea shows up
  // in the rich view. Going ``WYSIWYG → raw`` doesn't need a sync
  // because onUpdate already streams markdown into ``value``.
  // biome-ignore lint/correctness/useExhaustiveDependencies: ``value`` is intentionally NOT in deps — typing in raw mode would otherwise loop-reset the rich view on every keystroke.
  useEffect(() => {
    if (!rawMode && editor) {
      editor.commands.setContent(value);
    }
  }, [rawMode, editor]);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "0.4rem",
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        borderRadius: 6,
        background: "var(--bv-input-bg, #fff)",
      }}
    >
      <Toolbar
        editor={editor}
        rawMode={rawMode}
        onToggleRaw={() => setRawMode((v) => !v)}
        rawModeTitle={rawMode ? tEd("toggleToWysiwyg") : tEd("toggleToRaw")}
      />
      {errors && errors.length > 0 && (
        <div
          role="alert"
          style={{
            margin: "0 8px",
            padding: "8px 10px",
            background: "var(--bv-danger-soft, #fef2f2)",
            color: "var(--bv-danger, #b91c1c)",
            border: "1px solid var(--bv-danger, #b91c1c)",
            borderRadius: 4,
            fontSize: "0.82rem",
          }}
        >
          <strong style={{ display: "block", marginBottom: 4 }}>{tErr("title")}</strong>
          <p style={{ margin: "0 0 4px" }}>{tErr("explain")}</p>
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            {errors.map((v) => (
              <li key={v.raw}>
                {v.reason === "cross_patient"
                  ? tErr("violationCrossPatient", { raw: v.raw })
                  : tErr("violationNotFound", { raw: v.raw })}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div
        style={{
          padding: "8px 10px",
          minHeight: "120px",
          fontSize: "0.92rem",
        }}
      >
        {rawMode ? (
          <textarea
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={busy}
            style={{
              width: "100%",
              minHeight: 120,
              border: "none",
              padding: 0,
              background: "transparent",
              color: "var(--bv-fg)",
              fontFamily:
                "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
              fontSize: "0.88rem",
              lineHeight: 1.5,
              resize: "vertical",
              outline: "none",
            }}
          />
        ) : (
          <EditorContent editor={editor} />
        )}
      </div>
      {!embedded && (
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.4rem",
            padding: "6px 8px",
            borderTop: "1px solid var(--bv-divider, #eef0f3)",
          }}
        >
          <button
            type="button"
            className="ghost"
            onClick={onCancel}
            disabled={busy}
            title={cancelTitle}
            style={{ fontSize: "0.8rem" }}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={() => onSave && void onSave()}
            disabled={busy || value.trim().length === 0}
            title={saveTitle}
            style={{ fontSize: "0.8rem" }}
          >
            {busy && saveBusyLabel ? saveBusyLabel : saveLabel}
          </button>
        </div>
      )}
    </div>
  );
}

function Toolbar({
  editor,
  rawMode,
  onToggleRaw,
  rawModeTitle,
}: {
  editor: ReturnType<typeof useEditor> | null;
  rawMode: boolean;
  onToggleRaw: () => void;
  rawModeTitle: string;
}) {
  if (!editor) return null;
  // Formatting buttons are disabled in raw markdown mode because they
  // would target the (hidden) ProseMirror state instead of the
  // textarea the user is actually typing into.
  const fmtDisabled = rawMode;
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 2,
        padding: "4px 6px",
        borderBottom: "1px solid var(--bv-divider, #eef0f3)",
        background: "var(--bv-card-bg, #fff)",
      }}
    >
      <ToolbarButton
        editor={editor}
        active={editor.isActive("bold")}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleBold().run()}
        title="Bold (Ctrl+B)"
      >
        <strong>B</strong>
      </ToolbarButton>
      <ToolbarButton
        editor={editor}
        active={editor.isActive("italic")}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleItalic().run()}
        title="Italic (Ctrl+I)"
      >
        <em>I</em>
      </ToolbarButton>
      <ToolbarSeparator />
      <ToolbarButton
        editor={editor}
        active={editor.isActive("heading", { level: 1 })}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}
        title="Heading 1"
      >
        H1
      </ToolbarButton>
      <ToolbarButton
        editor={editor}
        active={editor.isActive("heading", { level: 2 })}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
        title="Heading 2"
      >
        H2
      </ToolbarButton>
      <ToolbarButton
        editor={editor}
        active={editor.isActive("heading", { level: 3 })}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}
        title="Heading 3"
      >
        H3
      </ToolbarButton>
      <ToolbarSeparator />
      <ToolbarButton
        editor={editor}
        active={editor.isActive("bulletList")}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleBulletList().run()}
        title="Bullet list"
      >
        ·≡
      </ToolbarButton>
      <ToolbarButton
        editor={editor}
        active={editor.isActive("orderedList")}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleOrderedList().run()}
        title="Ordered list"
      >
        1.
      </ToolbarButton>
      <ToolbarButton
        editor={editor}
        active={editor.isActive("blockquote")}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleBlockquote().run()}
        title="Quote"
      >
        ❝
      </ToolbarButton>
      <ToolbarButton
        editor={editor}
        active={editor.isActive("code")}
        disabled={fmtDisabled}
        onClick={() => editor.chain().focus().toggleCode().run()}
        title="Inline code"
      >
        {"</>"}
      </ToolbarButton>
      <span style={{ flex: 1 }} />
      <ToolbarButton editor={editor} active={rawMode} onClick={onToggleRaw} title={rawModeTitle}>
        {rawMode ? "WYSIWYG" : "MD"}
      </ToolbarButton>
    </div>
  );
}

function ToolbarSeparator() {
  return (
    <span
      aria-hidden
      style={{
        width: 1,
        background: "var(--bv-divider, #eef0f3)",
        margin: "0 4px",
      }}
    />
  );
}

function ToolbarButton({
  active,
  disabled,
  onClick,
  title,
  children,
}: {
  editor: ReturnType<typeof useEditor> | null;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      title={title}
      aria-pressed={active}
      disabled={disabled}
      style={{
        background: active ? "var(--bv-divider, #eef0f3)" : "transparent",
        color: "var(--bv-fg, #0f172a)",
        border: "1px solid transparent",
        borderRadius: 4,
        padding: "2px 8px",
        fontSize: "0.82rem",
        lineHeight: 1.2,
        cursor: disabled ? "not-allowed" : "pointer",
        minWidth: 28,
        opacity: disabled ? 0.45 : 1,
      }}
    >
      {children}
    </button>
  );
}
