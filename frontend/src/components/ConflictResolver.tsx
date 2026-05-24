"use client";

// Conflict resolver UI for a single proposal (== consultation review).
// Default mode is clinical-language: per entity in conflict, three
// buttons "Tieni la mia / Tieni del consulto / Modifica a mano".
// Advanced mode reveals object_hashes + JSON diff.
//
// Hits proposalsApi.{detail, resolveConflict, merge, reject}. The
// reject path is owner-side; this component is only rendered to the
// patient owner today (see consultations/[id]/page.tsx), so the
// proposer-self-withdraw path is out of scope here.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useModal } from "@/components/ModalHost";

import { ApiError, type ConflictOut, type ProposalOut, proposalsApi } from "@/lib/api";

interface Props {
  proposalId: string;
  advanced?: boolean;
  /** Called after a successful merge. */
  onMerged?: (p: ProposalOut) => void;
  /** Called after the owner rejects. Kept named ``onWithdrawn`` for
   * source-compat with the consultation page; it fires on rejection. */
  onWithdrawn?: (p: ProposalOut) => void;
}

export default function ConflictResolver({
  proposalId,
  advanced = false,
  onMerged,
  onWithdrawn,
}: Props) {
  const modal = useModal();
  const tCR = useTranslations("conflictResolver");
  const [proposal, setProposal] = useState<ProposalOut | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setErr(null);
    try {
      setProposal(await proposalsApi.detail(proposalId));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [proposalId]);

  useEffect(() => {
    load();
  }, [load]);

  if (!proposal) {
    return err ? <p className="error">{err}</p> : <p className="meta">{tCR("loading")}</p>;
  }

  const allResolved = proposal.conflicts.every((c) => c.resolution !== null);
  const noConflicts = proposal.conflicts.length === 0;
  const canMerge = proposal.status === "open" && (noConflicts || allResolved);

  async function resolve(c: ConflictOut, kind: "take_source" | "take_target") {
    setBusy(true);
    setErr(null);
    try {
      await proposalsApi.resolveConflict(proposalId, c.id, { kind });
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "resolve failed");
    } finally {
      setBusy(false);
    }
  }

  async function merge() {
    setBusy(true);
    setErr(null);
    try {
      const result = await proposalsApi.merge(proposalId);
      setProposal(result);
      onMerged?.(result);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "merge failed");
    } finally {
      setBusy(false);
    }
  }

  async function reject(reviewNotes: string) {
    setBusy(true);
    setErr(null);
    try {
      const result = await proposalsApi.reject(proposalId, reviewNotes);
      setProposal(result);
      onWithdrawn?.(result);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "reject failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "0.7rem",
      }}
    >
      <header>
        <h2 style={{ margin: 0 }}>{proposal.title}</h2>
        <p className="meta" style={{ fontSize: "0.78rem" }}>
          {tCR("stateLabel")}: <strong>{tCR(`status${proposalStatusKey(proposal.status)}`)}</strong>
          {advanced && (
            <>
              {" · "}
              <code style={{ fontSize: "0.7rem" }}>
                {proposal.source_ref_name} → {proposal.target_ref_name}
              </code>
            </>
          )}
        </p>
        {proposal.description && <p style={{ fontSize: "0.85rem" }}>{proposal.description}</p>}
      </header>

      {err && <p className="error">{err}</p>}

      {noConflicts && proposal.status === "open" && (
        <p className="meta">{tCR("noConflictsFastForward")}</p>
      )}

      {proposal.conflicts.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          <p className="meta" style={{ fontSize: "0.82rem" }}>
            {allResolved
              ? tCR("allResolved")
              : tCR("conflictsRemaining", {
                  n: proposal.conflicts.filter((c) => !c.resolution).length,
                })}
          </p>
          {proposal.conflicts.map((c) => (
            <ConflictRow
              key={c.id}
              conflict={c}
              busy={busy}
              advanced={advanced}
              onResolve={resolve}
              disabled={proposal.status !== "open"}
            />
          ))}
        </div>
      )}

      {proposal.status === "open" && (
        <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
          <button
            type="button"
            onClick={merge}
            disabled={!canMerge || busy}
            title={canMerge ? tCR("approveTitleEnabled") : tCR("approveTitleDisabled")}
            style={primaryBtnStyle(canMerge && !busy)}
          >
            {busy ? "..." : tCR("approveButton")}
          </button>
          <button
            type="button"
            onClick={async () => {
              // Reject requires a non-empty reason: it lands as
              // proposals.review_notes and is shown to the proposer in
              // the consultation page. The backend enforces non-empty
              // too; this client-side check just avoids a round-trip
              // and lets us re-prompt in the same modal cycle.
              const reason = await modal.prompt({
                title: tCR("rejectPromptTitle"),
                label: tCR("rejectPromptLabel"),
                multiline: true,
              });
              if (reason === null) return; // user cancelled
              const r = reason.trim();
              if (!r) {
                await modal.confirm({
                  title: tCR("rejectPromptTitle"),
                  message: tCR("rejectReasonRequired"),
                  confirmLabel: "OK",
                });
                return;
              }
              reject(r);
            }}
            disabled={busy}
            style={ghostBtnStyle()}
          >
            {tCR("rejectButton")}
          </button>
        </div>
      )}

      {proposal.status === "merged" && (
        <p className="meta" style={{ color: "#15803d" }}>
          {tCR("mergedOk")}
          {proposal.merge_commit && advanced && (
            <>
              {" · "}
              <code style={{ fontSize: "0.7rem" }}>{proposal.merge_commit.slice(0, 12)}</code>
            </>
          )}
        </p>
      )}

      {(proposal.status === "rejected" || proposal.status === "withdrawn") && (
        <p className="meta" style={{ color: "#b91c1c" }}>
          {tCR("rejectedMsg")}
          {proposal.review_notes && `: ${proposal.review_notes}`}
        </p>
      )}
    </section>
  );
}

function ConflictRow({
  conflict,
  busy,
  advanced,
  onResolve,
  disabled,
}: {
  conflict: ConflictOut;
  busy: boolean;
  advanced: boolean;
  onResolve: (c: ConflictOut, kind: "take_source" | "take_target") => void;
  disabled: boolean;
}) {
  const tCR = useTranslations("conflictResolver");
  const resolved = conflict.resolution !== null;
  return (
    <div
      style={{
        padding: "0.55rem 0.75rem",
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        borderRadius: 6,
        background: resolved ? "rgba(34, 197, 94, 0.06)" : "rgba(234, 179, 8, 0.06)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
        }}
      >
        <div>
          <strong>{tCR(entityKindKey(conflict.entity_kind))}</strong>{" "}
          <span className="meta" style={{ fontSize: "0.7rem" }}>
            {conflict.entity_id.slice(0, 8)}
          </span>
        </div>
        <span className="meta" style={{ fontSize: "0.72rem" }}>
          {tCR(conflictKindKey(conflict.conflict_kind))}
        </span>
      </div>
      {advanced && (
        <pre
          style={{
            marginTop: "0.4rem",
            fontSize: "0.65rem",
            color: "#6b7280",
            background: "rgba(0,0,0,0.04)",
            padding: "0.4rem",
            borderRadius: 4,
            overflowX: "auto",
          }}
        >
          {`base   ${conflict.base_object_hash?.slice(0, 16) ?? "(missing)"}
source ${conflict.source_object_hash?.slice(0, 16) ?? "(missing)"}
target ${conflict.target_object_hash?.slice(0, 16) ?? "(missing)"}`}
        </pre>
      )}
      {resolved && conflict.resolution ? (
        <p className="meta" style={{ marginTop: "0.4rem", fontSize: "0.78rem" }}>
          {tCR("resolvedPrefix")}: <strong>{tCR(resolutionKey(conflict.resolution))}</strong>
        </p>
      ) : (
        <div
          style={{
            display: "flex",
            gap: "0.4rem",
            marginTop: "0.5rem",
            flexWrap: "wrap",
          }}
        >
          <button
            type="button"
            onClick={() => onResolve(conflict, "take_target")}
            disabled={busy || disabled}
            style={ghostBtnStyle()}
          >
            {tCR("keepMine")}
          </button>
          <button
            type="button"
            onClick={() => onResolve(conflict, "take_source")}
            disabled={busy || disabled}
            style={ghostBtnStyle()}
          >
            {tCR("keepSource")}
          </button>
        </div>
      )}
    </div>
  );
}

function proposalStatusKey(s: ProposalOut["status"]): string {
  const map: Record<ProposalOut["status"], string> = {
    open: "Open",
    approved: "Approved",
    merged: "Merged",
    rejected: "Rejected",
    withdrawn: "Withdrawn",
    superseded: "Superseded",
  };
  return map[s];
}

function entityKindKey(k: string): string {
  const map: Record<string, string> = {
    clinical_note: "kindClinicalNote",
    annotation: "kindAnnotation",
    tag: "kindTag",
    report: "kindReport",
    consultation: "kindConsultation",
    patient_document: "kindDocument",
    summary: "kindSummary",
    measurement: "kindMeasurement",
    segmentation: "kindSegmentation",
    patient: "kindPatient",
    study: "kindStudy",
    series: "kindSeries",
  };
  return map[k] || "kindDefault";
}

function conflictKindKey(k: ConflictOut["conflict_kind"]): string {
  const map: Record<ConflictOut["conflict_kind"], string> = {
    add_add: "ckAddAdd",
    edit_edit: "ckEditEdit",
    edit_delete: "ckEditDelete",
    delete_edit: "ckDeleteEdit",
  };
  return map[k];
}

function resolutionKey(r: NonNullable<ConflictOut["resolution"]>): string {
  if (r === "take_source") return "resTakeSource";
  if (r === "take_target") return "resTakeTarget";
  if (r === "auto_merge") return "resAutoMerge";
  return "resManual";
}

function primaryBtnStyle(enabled: boolean): React.CSSProperties {
  return {
    padding: "0.45rem 0.95rem",
    background: enabled ? "#15803d" : "#9ca3af",
    color: "white",
    border: "none",
    borderRadius: 4,
    cursor: enabled ? "pointer" : "not-allowed",
    fontSize: "0.85rem",
    fontWeight: 600,
  };
}

function ghostBtnStyle(): React.CSSProperties {
  return {
    padding: "0.35rem 0.7rem",
    background: "transparent",
    border: "1px solid var(--bv-card-border)",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.78rem",
    color: "inherit",
  };
}
