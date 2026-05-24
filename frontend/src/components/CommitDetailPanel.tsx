"use client";

// Detail panel for a single commit. Shared between the patient
// history drawer (where ``onBack`` is set so a back-to-timeline link
// renders) and the full advanced history page (where the timeline
// stays visible on the left, so onBack is omitted).
//
// Surfaces the commit metadata, the author badge, the "Annulla questa
// revisione" (revert) action, and the per-entity restore action that
// undoes one entity to its state at this commit.

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import AuthorBadge from "@/components/AuthorBadge";
import { useModal } from "@/components/ModalHost";
import { ApiError, type CommitOut, type RevertConflictDetail, historyApi } from "@/lib/api";

// Keys must mirror the ``entityKinds`` namespace in messages/{it,en}.json.
// Used as a guard before calling ``tEnt(...)`` so unknown kinds fall back
// to the raw kind string rather than triggering a missing-translation
// warning at runtime.
export const KNOWN_ENTITY_KINDS_LIST = [
  "clinical_note",
  "annotation",
  "annotation_dicom",
  "tag",
  "report",
  "consultation",
  "patient_document",
  "summary",
  "measurement",
  "segmentation",
  "patient",
  "study",
  "series",
] as const;
const KNOWN_ENTITY_KINDS: ReadonlySet<string> = new Set(KNOWN_ENTITY_KINDS_LIST);

interface Props {
  patientId: string;
  commit: CommitOut;
  advanced: boolean;
  /** When set, renders a "← back to timeline" link at the top. The
   * drawer needs it (it switches between timeline and detail views in
   * the same column); the full history page does not (timeline is
   * always visible in the left column). */
  onBack?: () => void;
  /** Called after a successful revert / restore so the parent can
   * refresh the timeline and reset selection state. */
  onMutated: () => void;
}

export default function CommitDetailPanel({
  patientId,
  commit,
  advanced,
  onBack,
  onMutated,
}: Props) {
  const tH = useTranslations("historyPage");
  const tEnt = useTranslations("entityKinds");
  const labelForKind = (kind: string): string => {
    if (KNOWN_ENTITY_KINDS.has(kind)) return tEnt(kind as (typeof KNOWN_ENTITY_KINDS_LIST)[number]);
    return kind;
  };
  const modal = useModal();
  const [state, setState] = useState<Record<
    string,
    Record<string, Record<string, unknown>>
  > | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    historyApi
      .atCommit(patientId, commit.commit_hash)
      .then((data) => {
        if (!cancelled) {
          setState(data);
          setErr(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, commit.commit_hash]);

  const onRevertClick = async () => {
    if (busy) return;
    const ok = await modal.confirm({
      title: tH("confirmRevertTitle"),
      message: tH("confirmRevertBody", {
        message: commit.message || tH("detailNoMessage"),
      }),
      confirmLabel: tH("revertSubmit"),
      destructive: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await historyApi.revert(patientId, commit.commit_hash, {
        message: `Revert: ${commit.message || tH("detailNoMessage")}`,
      });
      onMutated();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const detail = e.detail as { detail?: RevertConflictDetail } | RevertConflictDetail;
        const conflictDetail =
          (detail as { detail?: RevertConflictDetail }).detail ?? (detail as RevertConflictDetail);
        const conflictLines = (conflictDetail?.conflicts ?? [])
          .map((c) => `- ${c.entity_kind} ${c.entity_id.slice(0, 8)}`)
          .join("\n");
        await modal.confirm({
          title: tH("revertConflictTitle"),
          message: `${tH("revertConflictBody")}\n\n${tH("revertConflictListHeader")}\n${conflictLines}`,
          confirmLabel: "OK",
        });
      } else {
        await modal.confirm({
          title: tH("revertFailed"),
          message: e instanceof ApiError ? e.message : String(e),
          confirmLabel: "OK",
        });
      }
    } finally {
      setBusy(false);
    }
  };

  const onRestoreEntity = async (entityKind: string, entityId: string) => {
    if (busy) return;
    const ok = await modal.confirm({
      title: tH("confirmRestoreTitle"),
      message: tH("confirmRestoreBody", {
        kind: labelForKind(entityKind),
        message: commit.message || tH("detailNoMessage"),
      }),
      confirmLabel: tH("restoreSubmit"),
      destructive: false,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await historyApi.restoreEntity(patientId, {
        source_commit_hash: commit.commit_hash,
        entity_kind: entityKind,
        entity_id: entityId,
        message: `Restore ${entityKind} from: ${commit.message || tH("detailNoMessage")}`,
      });
      onMutated();
    } catch (e) {
      await modal.confirm({
        title: tH("restoreFailed"),
        message: e instanceof ApiError ? e.message : String(e),
        confirmLabel: "OK",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      {onBack && (
        <button
          type="button"
          onClick={onBack}
          style={{
            background: "transparent",
            border: "none",
            color: "var(--bv-muted)",
            padding: 0,
            cursor: "pointer",
            fontSize: "0.8rem",
            marginBottom: "0.5rem",
          }}
        >
          {tH("detailBack")}
        </button>
      )}
      <h3 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>
        {commit.message || tH("detailNoMessage")}
      </h3>
      <div style={{ marginBottom: "0.6rem" }}>
        <AuthorBadge commit={commit} advanced={advanced} size="md" />
      </div>
      <p className="meta" style={{ fontSize: "0.72rem", margin: "0 0 0.7rem" }}>
        {new Date(commit.created_at).toLocaleString()}
        {advanced && (
          <>
            {" · "}
            <code style={{ fontSize: "0.65rem" }}>{commit.commit_hash.slice(0, 12)}</code>
          </>
        )}
      </p>

      <div style={{ marginBottom: "0.8rem" }}>
        <button
          type="button"
          onClick={onRevertClick}
          disabled={busy}
          title={tH("actionRevertTitle")}
          style={{
            border: "1px solid #b91c1c",
            color: "#b91c1c",
            background: "transparent",
            padding: "0.35rem 0.7rem",
            borderRadius: 4,
            fontSize: "0.78rem",
            cursor: busy ? "wait" : "pointer",
            opacity: busy ? 0.6 : 1,
          }}
        >
          {tH("actionRevert")}
        </button>
      </div>

      {loading && <p className="meta">{tH("detailLoadingState")}</p>}
      {err && <p className="error">{err}</p>}

      {state && Object.keys(state).length === 0 && <p className="meta">{tH("detailEmpty")}</p>}

      {state &&
        Object.entries(state).map(([kind, entities]) => (
          <section key={kind} style={{ marginBottom: "0.8rem" }}>
            <h4 style={{ fontSize: "0.82rem", margin: "0 0 0.3rem" }}>
              {labelForKind(kind)} ({Object.keys(entities).length})
            </h4>
            <ul
              style={{
                listStyle: "none",
                margin: 0,
                padding: 0,
                display: "flex",
                flexDirection: "column",
                gap: "0.3rem",
              }}
            >
              {Object.entries(entities).map(([eid, payload]) => (
                <li
                  key={eid}
                  style={{
                    padding: "0.4rem 0.55rem",
                    border: "1px solid var(--bv-card-border, #e5e7eb)",
                    borderRadius: 4,
                    fontSize: "0.78rem",
                    background: "rgba(0,0,0,0.02)",
                  }}
                >
                  <EntitySummary
                    kind={kind}
                    payload={payload}
                    advanced={advanced}
                    eid={eid}
                    onRestore={() => onRestoreEntity(kind, eid)}
                    busy={busy}
                  />
                </li>
              ))}
            </ul>
          </section>
        ))}
    </div>
  );
}

function EntitySummary({
  kind,
  payload,
  advanced,
  eid,
  onRestore,
  busy,
}: {
  kind: string;
  payload: Record<string, unknown>;
  advanced: boolean;
  eid: string;
  onRestore: () => void;
  busy: boolean;
}) {
  const tH = useTranslations("historyPage");
  const isTombstoned = Boolean(payload._tombstoned);
  if (isTombstoned) {
    return <span style={{ color: "#b91c1c" }}>{tH("tombstoned")}</span>;
  }
  const summary = <SummaryFor kind={kind} payload={payload} />;
  return (
    <div>
      <div style={{ display: "flex", alignItems: "flex-start", gap: "0.5rem" }}>
        <div style={{ flex: 1, minWidth: 0 }}>{summary}</div>
        <button
          type="button"
          className="ghost"
          onClick={onRestore}
          disabled={busy}
          title={tH("actionRestoreTitle")}
          style={{
            padding: "0.2rem 0.5rem",
            borderRadius: 3,
            fontSize: "0.7rem",
            cursor: busy ? "wait" : "pointer",
            opacity: busy ? 0.6 : 1,
            whiteSpace: "nowrap",
          }}
        >
          {tH("actionRestore")}
        </button>
      </div>
      {advanced && (
        <pre
          style={{
            marginTop: "0.3rem",
            fontSize: "0.65rem",
            background: "rgba(0,0,0,0.04)",
            padding: "0.35rem",
            borderRadius: 3,
            overflowX: "auto",
            color: "#374151",
          }}
        >
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
      {!advanced && (
        <div className="meta" style={{ fontSize: "0.66rem", marginTop: "0.15rem" }}>
          {eid.slice(0, 8)}
        </div>
      )}
    </div>
  );
}

function SummaryFor({
  kind,
  payload,
}: {
  kind: string;
  payload: Record<string, unknown>;
}): React.ReactNode {
  const tH = useTranslations("historyPage");
  if (kind === "clinical_note") {
    const body = (payload.body as string) ?? "";
    const target = (payload.target_kind as string) ?? "";
    const label = target ? tH("summaryNoteOn", { target }) : tH("summaryNote");
    return (
      <>
        <strong>{label}:</strong>{" "}
        <span>{body.length > 140 ? `${body.slice(0, 140)}...` : body}</span>
      </>
    );
  }
  if (kind === "patient") {
    const name = (payload.display_name as string) ?? "";
    return <span>{name || tH("summaryDemographics")}</span>;
  }
  if (kind === "tag") {
    const ns = (payload.namespace as string) ?? "";
    const v = (payload.value as string) ?? "";
    return (
      <span>
        <code style={{ fontSize: "0.7rem" }}>
          {ns}:{v}
        </code>
      </span>
    );
  }
  if (kind === "report") {
    const title = (payload.title as string) ?? (payload.text as string)?.slice(0, 80) ?? "";
    return <strong>{title}</strong>;
  }
  if (kind === "consultation") {
    const title = (payload.title as string) ?? "";
    return <strong>{title || tH("summaryConsultation")}</strong>;
  }
  return <span style={{ color: "var(--bv-muted)" }}>{kind}</span>;
}
