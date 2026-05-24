"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useState } from "react";

import CommitDetailPanel from "@/components/CommitDetailPanel";
import CommitDiffViewer from "@/components/CommitDiffViewer";
import HistoryTimeline from "@/components/HistoryTimeline";
import type { CommitOut } from "@/lib/api";

export default function PatientHistoryPage() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const advanced = search.get("advanced") === "1";
  const [selected, setSelected] = useState<CommitOut | null>(null);
  const [compareTo, setCompareTo] = useState<CommitOut | null>(null);
  // Bumped after a successful revert/restore so the timeline remounts
  // and refetches; keeps state in sync without prop-drilling a refresh
  // callback into HistoryTimeline.
  const [refreshKey, setRefreshKey] = useState(0);
  const tH = useTranslations("historyPage");

  const onMutated = () => {
    setSelected(null);
    setCompareTo(null);
    setRefreshKey((k) => k + 1);
  };

  const onClearSelection = () => {
    setSelected(null);
    setCompareTo(null);
  };

  // Selection-as-set: the timeline highlights both the primary and
  // (when present) the compare-with row.
  const selectedHashes: string[] = [];
  if (selected) selectedHashes.push(selected.commit_hash);
  if (compareTo) selectedHashes.push(compareTo.commit_hash);

  return (
    <main>
      <p className="meta">
        <Link href={`/patients/${params.id}`}>{tH("back")}</Link>
      </p>
      <h1>{tH("title")}</h1>
      <p className="meta">
        {tH("intro")}
        {!advanced && (
          <>
            {" "}
            <Link href={"?advanced=1"} style={{ color: "var(--bv-muted)", fontSize: "0.78rem" }}>
              {tH("advancedToggle")}
            </Link>
          </>
        )}
        {advanced && (
          <>
            {" "}
            <Link href={"?"} style={{ color: "var(--bv-muted)", fontSize: "0.78rem" }}>
              {tH("backToClinical")}
            </Link>
          </>
        )}
      </p>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(280px, 1fr) 2fr",
          gap: "1.2rem",
          marginTop: "1rem",
        }}
      >
        <aside>
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              justifyContent: "space-between",
              gap: "0.4rem",
            }}
          >
            <h2 style={{ fontSize: "0.95rem", margin: 0 }}>{tH("asideTitle")}</h2>
            {(selected || compareTo) && (
              <button
                type="button"
                className="ghost"
                onClick={onClearSelection}
                style={{ padding: "0.15rem 0.5rem", fontSize: "0.7rem" }}
              >
                {tH("clearSelection")}
              </button>
            )}
          </div>
          <HistoryTimeline
            key={refreshKey}
            patientId={params.id}
            advanced={advanced}
            branchAware
            selectedHashes={selectedHashes}
            onSelect={(c) => {
              if (!selected) {
                setSelected(c);
                setCompareTo(null);
                return;
              }
              if (c.commit_hash === selected.commit_hash) {
                // Same commit clicked again → drop the compare and
                // keep it as primary selection (or clear if there was
                // no compare).
                setCompareTo(null);
                return;
              }
              if (compareTo && c.commit_hash === compareTo.commit_hash) {
                // Same compare commit clicked again → drop the
                // compare back to single-selection.
                setCompareTo(null);
                return;
              }
              // Second distinct commit picks the compare slot.
              setCompareTo(c);
            }}
          />
        </aside>
        <section>
          {!selected && <p className="meta">{tH("selectFromTimeline")}</p>}
          {selected && !compareTo && (
            <SelectedDetail
              patientId={params.id}
              commit={selected}
              advanced={advanced}
              onMutated={onMutated}
            />
          )}
          {selected && compareTo && (
            <DiffPanel
              patientId={params.id}
              from={selected}
              to={compareTo}
              advanced={advanced}
              onClear={() => setCompareTo(null)}
            />
          )}
        </section>
      </div>
    </main>
  );
}

function SelectedDetail({
  patientId,
  commit,
  advanced,
  onMutated,
}: {
  patientId: string;
  commit: CommitOut;
  advanced: boolean;
  onMutated: () => void;
}) {
  const tH = useTranslations("historyPage");
  return (
    <div className="card" style={{ padding: "0.9rem" }}>
      <CommitDetailPanel
        patientId={patientId}
        commit={commit}
        advanced={advanced}
        onMutated={onMutated}
      />
      {advanced && (
        <pre
          style={{
            marginTop: "0.7rem",
            fontSize: "0.7rem",
            background: "rgba(0,0,0,0.04)",
            padding: "0.5rem",
            borderRadius: 4,
            overflowX: "auto",
          }}
        >
          {`commit ${commit.commit_hash}
tree   ${commit.tree_hash}
parent ${commit.parent_hashes.join(" ") || "(none)"}
branch ${commit.branch_at_creation ?? "(unknown)"}`}
        </pre>
      )}
      <p
        className="meta"
        style={{
          marginTop: "0.9rem",
          fontSize: "0.78rem",
          padding: "0.5rem 0.7rem",
          background: "var(--bv-info-soft, #eef4ff)",
          borderLeft: "3px solid var(--bv-info, #1e40af)",
          borderRadius: 3,
        }}
      >
        {tH("compareHintSelected")}
      </p>
    </div>
  );
}

function DiffPanel({
  patientId,
  from,
  to,
  advanced,
  onClear,
}: {
  patientId: string;
  from: CommitOut;
  to: CommitOut;
  advanced: boolean;
  onClear: () => void;
}) {
  const tH = useTranslations("historyPage");
  // Order so 'from' is older and 'to' newer (UX expectation).
  const fromDate = new Date(from.created_at).getTime();
  const toDate = new Date(to.created_at).getTime();
  const olderFirst = fromDate <= toDate ? from : to;
  const newerSecond = fromDate <= toDate ? to : from;
  const fromTxt = new Date(olderFirst.created_at).toLocaleString();
  const toTxt = new Date(newerSecond.created_at).toLocaleString();

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          marginBottom: "0.7rem",
        }}
      >
        <h2 style={{ fontSize: "0.95rem", margin: 0 }}>{tH("compareTitle")}</h2>
        <button
          type="button"
          className="ghost"
          onClick={onClear}
          style={{
            padding: "0.2rem 0.6rem",
            fontSize: "0.75rem",
          }}
        >
          {tH("compareClose")}
        </button>
      </div>
      <p className="meta" style={{ fontSize: "0.78rem" }}>
        {tH.rich("compareFromTo", {
          strong: (chunks) => <strong>{chunks}</strong>,
          from: fromTxt,
          to: toTxt,
        })}
      </p>
      <CommitDiffViewer
        patientId={patientId}
        fromCommit={olderFirst.commit_hash}
        toCommit={newerSecond.commit_hash}
        advanced={advanced}
      />
    </div>
  );
}
