"use client";

// Timeline of commits on a patient ref. Designed to feel Drive-like
// for default users (date + author + clinical-language summary), with
// hash + branch info exposed only when the ?advanced=1 flag is on
// or the user has versioning:advanced via their profile.
//
// Default mode is "leggibile": shows the message message-only, an icon
// for human / AI / system, and a relative timestamp. Clicking a row
// expands to reveal the diff vs the previous commit.

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import AuthorBadge from "@/components/AuthorBadge";
import { ApiError, type CommitOut, historyApi } from "@/lib/api";

interface Props {
  patientId: string;
  refName?: string;
  /** When true, reveals commit hashes / parent / tree info. */
  advanced?: boolean;
  /** Clicking a commit row pings this callback. The viewer can swap to
   *  time-travel mode and load /at/<hash>. Optional. */
  onSelect?: (commit: CommitOut) => void;
  /** When true, fetch commits from every branch (main + each
   * consultation/<id>) merged into one time-sorted list. The row UI
   * paints a small chip on commits that did not originate on main. */
  branchAware?: boolean;
  /** Hex hashes of commits to highlight as currently selected. The
   * first entry renders as the primary selection (filled accent),
   * subsequent entries (typically the compare-with target) use a
   * lighter outline. Order matters for the visual treatment. */
  selectedHashes?: string[];
}

export default function HistoryTimeline({
  patientId,
  refName = "main",
  advanced = false,
  onSelect,
  branchAware = false,
  selectedHashes = [],
}: Props) {
  const tUi = useTranslations("uiCommon");
  const [commits, setCommits] = useState<CommitOut[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const promise = branchAware
      ? historyApi.listAll(patientId, { per_ref_limit: 100 }).then((data) => data.commits)
      : historyApi.list(patientId, { ref: refName, limit: 100 }).then((data) => data.commits);
    promise
      .then((data) => {
        if (cancelled) return;
        setCommits(data);
        setErr(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof ApiError ? e.message : "history load failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, refName, branchAware]);

  if (loading) return <p className="meta">{tUi("loadingHistory")}</p>;
  if (err) return <p className="error">{err}</p>;
  if (!commits || commits.length === 0) return <p className="meta">{tUi("noRevisions")}</p>;

  return (
    <ol
      style={{
        listStyle: "none",
        padding: 0,
        margin: 0,
        display: "flex",
        flexDirection: "column",
        gap: "0.45rem",
      }}
    >
      {commits.map((c) => {
        const selectionIndex = selectedHashes.indexOf(c.commit_hash);
        return (
          <CommitRow
            key={c.commit_hash}
            commit={c}
            advanced={advanced}
            onSelect={onSelect}
            selectionRole={
              selectionIndex === 0 ? "primary" : selectionIndex > 0 ? "compare" : "none"
            }
          />
        );
      })}
    </ol>
  );
}

function CommitRow({
  commit,
  advanced,
  onSelect,
  selectionRole,
}: {
  commit: CommitOut;
  advanced: boolean;
  onSelect?: (c: CommitOut) => void;
  selectionRole: "none" | "primary" | "compare";
}) {
  const formatWhen = useFormatWhen();
  const tH = useTranslations("historyPage");
  const when = formatWhen(commit.created_at);
  const isAi = commit.author_kind === "agent";
  const branchAtCreation = commit.branch_at_creation ?? "";
  const isConsultation = branchAtCreation.startsWith("consultation/");
  // Pick a left-rail colour: AI commits win over consultation lane
  // when both apply (AI provenance is the more clinically salient cue).
  const railColor = isAi ? "#f59e0b" : isConsultation ? "#7c3aed" : "transparent";

  // Selection visual treatment. Primary uses the accent fill so the
  // user sees what they have picked first; compare uses an info tint
  // so the second pick reads as "I'm comparing this with the primary".
  const selectionStyles: React.CSSProperties =
    selectionRole === "primary"
      ? {
          background: "var(--bv-accent-soft, #fff3e8)",
          outline: "2px solid var(--bv-accent, #e96b1f)",
          outlineOffset: "-2px",
        }
      : selectionRole === "compare"
        ? {
            background: "var(--bv-info-soft, #eef4ff)",
            outline: "2px dashed var(--bv-info, #1e40af)",
            outlineOffset: "-2px",
          }
        : {};

  return (
    <li
      onClick={onSelect ? () => onSelect(commit) : undefined}
      onKeyDown={
        onSelect
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(commit);
              }
            }
          : undefined
      }
      role={onSelect ? "button" : undefined}
      tabIndex={onSelect ? 0 : undefined}
      aria-selected={selectionRole !== "none"}
      style={{
        padding: "0.55rem 0.75rem",
        // AI commits get a colored left rail so the row reads as
        // machine-generated even before the badge is parsed.
        // Consultation-branch commits get a distinct purple rail so
        // the user sees that the row is part of an open consult.
        borderLeft: `3px solid ${railColor}`,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        borderRadius: 6,
        background: "var(--bv-card-bg, #fff)",
        cursor: onSelect ? "pointer" : "default",
        display: "flex",
        flexDirection: "column",
        gap: "0.35rem",
        ...selectionStyles,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
        <AuthorBadge commit={commit} advanced={advanced} size="sm" />
        {isConsultation && (
          <span
            title={advanced ? branchAtCreation : tH("branchConsultTitle")}
            style={{
              fontSize: "0.66rem",
              padding: "0.1rem 0.4rem",
              borderRadius: 999,
              background: "#ede9fe",
              color: "#6d28d9",
              border: "1px solid #ddd6fe",
              fontWeight: 600,
              whiteSpace: "nowrap",
            }}
          >
            {tH("branchConsultChip")}
          </span>
        )}
        <span
          className="meta"
          style={{
            fontSize: "0.7rem",
            marginLeft: "auto",
            whiteSpace: "nowrap",
          }}
        >
          {when}
        </span>
      </div>
      <div style={{ fontSize: "0.88rem" }}>{commit.message || tH("detailNoMessage")}</div>
      {advanced && (
        <div
          className="meta"
          style={{
            fontFamily: "monospace",
            fontSize: "0.68rem",
            color: "#6b7280",
          }}
        >
          {commit.commit_hash.slice(0, 12)}
          {commit.parent_hashes.length > 0 && (
            <>
              {" ← "}
              {commit.parent_hashes.map((p) => p.slice(0, 8)).join(", ")}
            </>
          )}
          {commit.branch_at_creation && (
            <>
              {" · "}
              <span style={{ color: "#9ca3af" }}>{commit.branch_at_creation}</span>
            </>
          )}
        </div>
      )}
    </li>
  );
}

function useFormatWhen(): (iso: string) => string {
  const tRel = useTranslations("relativeTime");
  return (iso: string) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const now = Date.now();
    const diff = now - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return tRel("justNow");
    if (mins < 60) return tRel("minutes", { n: mins });
    const hours = Math.floor(mins / 60);
    if (hours < 24) return tRel("hours", { n: hours });
    const days = Math.floor(hours / 24);
    if (days < 7) return tRel("days", { n: days });
    return d.toLocaleDateString();
  };
}
