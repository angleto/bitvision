"use client";

// Visual identity of a commit's author: a small avatar plus a name and
// an optional secondary line. The default rendering is clinical-friendly
// (display_name for humans, AI assistant label for agents, "Sistema" for
// system commits). The advanced flag exposes model_id under the AI label
// for technical reviewers.

import { useTranslations } from "next-intl";

import type { CommitOut } from "@/lib/api";

type Size = "sm" | "md";

interface Props {
  commit: Pick<
    CommitOut,
    | "author_kind"
    | "author_display_name"
    | "model_id"
    | "provider"
    | "agent_assistant_id"
    | "agent_assistant_label"
    | "share_link_id"
    | "share_link_label"
    | "share_link_recipient"
  >;
  /** When true, exposes model_id, provider and the assistant id next
   *  to the AI label so a reviewer can correlate the commit with a
   *  specific assistant row even after the human-readable label was
   *  renamed. */
  advanced?: boolean;
  size?: Size;
}

export default function AuthorBadge({ commit, advanced = false, size = "sm" }: Props) {
  const tH = useTranslations("historyPage");

  const palette = paletteFor(commit.author_kind);
  const initials = avatarInitialsFor(commit);
  const primary = primaryNameFor(commit, tH);
  const secondary = secondaryLineFor(commit, advanced, tH);

  const sz = sizeFor(size);

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: sz.gap,
        minWidth: 0,
      }}
    >
      <span
        aria-hidden="true"
        title={tH(`authorKind.${commit.author_kind}` as const)}
        style={{
          width: sz.avatar,
          height: sz.avatar,
          borderRadius: "50%",
          background: palette.bg,
          color: palette.fg,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: sz.initials,
          fontWeight: 600,
          flexShrink: 0,
          border: `1px solid ${palette.border}`,
        }}
      >
        {initials}
      </span>
      <span
        style={{
          display: "inline-flex",
          flexDirection: "column",
          minWidth: 0,
          lineHeight: 1.1,
        }}
      >
        <span
          style={{
            fontSize: sz.primary,
            fontWeight: 600,
            color: palette.text,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {primary}
        </span>
        {secondary && (
          <span
            style={{
              fontSize: sz.secondary,
              color: "var(--bv-muted, #6b7280)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {secondary}
          </span>
        )}
      </span>
    </span>
  );
}

function paletteFor(kind: "human" | "agent" | "system" | "link"): {
  bg: string;
  fg: string;
  border: string;
  text: string;
} {
  if (kind === "agent") {
    // AI: warm amber so the row reads as machine-generated at a glance.
    return {
      bg: "#fef3c7",
      fg: "#92400e",
      border: "#fcd34d",
      text: "#92400e",
    };
  }
  if (kind === "system") {
    return {
      bg: "#e5e7eb",
      fg: "#4b5563",
      border: "#d1d5db",
      text: "#4b5563",
    };
  }
  if (kind === "link") {
    // Token-only share link ("modality A"). Picked deliberately
    // outside the human-blue / agent-amber / system-grey palette so
    // a reviewer scanning the timeline sees these rows pop.
    return {
      bg: "#fee2e2",
      fg: "#991b1b",
      border: "#fca5a5",
      text: "#7f1d1d",
    };
  }
  // Human: cool slate; intentionally NOT the same hue as AI so the
  // distinction is reliable for color-vision-deficient readers too
  // (the avatar shape + label still carry the meaning).
  return {
    bg: "#dbeafe",
    fg: "#1d4ed8",
    border: "#93c5fd",
    text: "#1e3a8a",
  };
}

function avatarInitialsFor(commit: Props["commit"]): string {
  if (commit.author_kind === "agent") return "AI";
  if (commit.author_kind === "system") return "S";
  if (commit.author_kind === "link") return "L";
  const name = commit.author_display_name?.trim();
  if (!name) return "?";
  // First letter of first two whitespace-separated tokens. Robust to
  // ``Dr. Maria Rossi`` (DM), ``rossi`` (R), ``a b c`` (AB).
  const tokens = name.split(/\s+/).filter(Boolean).slice(0, 2);
  const inits = tokens.map((t) => t.charAt(0).toUpperCase()).join("");
  return inits || "?";
}

function primaryNameFor(commit: Props["commit"], tH: ReturnType<typeof useTranslations>): string {
  if (commit.author_kind === "system") return tH("systemAuthor");
  if (commit.author_kind === "agent") {
    return commit.agent_assistant_label?.trim() || tH("aiUnnamedAssistant");
  }
  if (commit.author_kind === "link") {
    // Prefer the recipient name captured at share-create time; fall
    // back to the share label, then to a generic placeholder. The
    // commit's ``author_display_name`` for link-mode is the synthetic
    // PUBLIC subject's name, which would otherwise read as
    // "Pubblico" — useless for an audit row.
    const r = commit.share_link_recipient?.trim();
    if (r) return r;
    const l = commit.share_link_label?.trim();
    if (l) return l;
    return tH("linkAuthor");
  }
  return commit.author_display_name?.trim() || tH("unknownAuthor");
}

function secondaryLineFor(
  commit: Props["commit"],
  advanced: boolean,
  tH: ReturnType<typeof useTranslations>,
): string | null {
  if (commit.author_kind === "agent") {
    // Default: surface that this is AI-generated even when an assistant
    // label is present (the label can be ambiguous, e.g. "GPT comparison").
    // In advanced mode add the model + provider plus a short assistant
    // id (8 hex prefix) so a reviewer can disambiguate two assistants
    // sharing the same human label, or correlate the row with an audit
    // record after a rename.
    if (advanced) {
      const assistantTag = commit.agent_assistant_id
        ? `#${commit.agent_assistant_id.slice(0, 8)}`
        : null;
      const tail = [commit.model_id, commit.provider, assistantTag].filter(Boolean).join(" · ");
      return tail ? `${tH("aiAuthorPrefix")} · ${tail}` : tH("aiAuthorPrefix");
    }
    return tH("aiAuthorPrefix");
  }
  if (commit.author_kind === "link") {
    // Always surface that this came from a token-only link so the
    // reviewer is reminded the writer was unauthenticated even when
    // a recipient name is on file.
    return tH("linkAuthorPrefix");
  }
  return null;
}

function sizeFor(size: Size): {
  avatar: string;
  initials: string;
  primary: string;
  secondary: string;
  gap: string;
} {
  if (size === "md") {
    return {
      avatar: "1.7rem",
      initials: "0.72rem",
      primary: "0.88rem",
      secondary: "0.72rem",
      gap: "0.5rem",
    };
  }
  return {
    avatar: "1.35rem",
    initials: "0.6rem",
    primary: "0.78rem",
    secondary: "0.66rem",
    gap: "0.4rem",
  };
}
