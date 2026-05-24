"use client";

// Side-by-side diff between two commits of a patient. Default rendering
// is "clinical language" ("Aggiunta una nota su X", "Modificato il
// referto Y"); advanced mode shows the JSON payloads side-by-side
// for fine-grained inspection.

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { ApiError, type DiffEntryOut, historyApi } from "@/lib/api";

const KNOWN_DIFF_KINDS_LIST = [
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
const KNOWN_DIFF_KINDS: ReadonlySet<string> = new Set(KNOWN_DIFF_KINDS_LIST);

interface Props {
  patientId: string;
  fromCommit: string;
  toCommit: string;
  advanced?: boolean;
}

export default function CommitDiffViewer({
  patientId,
  fromCommit,
  toCommit,
  advanced = false,
}: Props) {
  const tH = useTranslations("historyPage");
  const tEnt = useTranslations("entityKinds");
  const labelForKind = (k: string): string =>
    KNOWN_DIFF_KINDS.has(k) ? tEnt(k as (typeof KNOWN_DIFF_KINDS_LIST)[number]) : k;
  const labelForChange = (c: "added" | "removed" | "modified"): string => {
    if (c === "added") return tH("diffAdded");
    if (c === "removed") return tH("diffRemoved");
    return tH("diffModified");
  };
  const [entries, setEntries] = useState<DiffEntryOut[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    historyApi
      .diff(patientId, fromCommit, toCommit)
      .then((data) => {
        if (!cancelled) setEntries(data);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "diff failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, fromCommit, toCommit]);

  if (loading) return <p className="meta">{tH("diffLoading")}</p>;
  if (err) return <p className="error">{err}</p>;
  if (!entries || entries.length === 0) return <p className="meta">{tH("diffEmpty")}</p>;

  // Group by entity_kind for cleaner reading.
  const byKind: Record<string, DiffEntryOut[]> = {};
  for (const e of entries) {
    let bucket = byKind[e.entity_kind];
    if (!bucket) {
      bucket = [];
      byKind[e.entity_kind] = bucket;
    }
    bucket.push(e);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.7rem" }}>
      {Object.entries(byKind).map(([kind, rows]) => (
        <section key={kind}>
          <h3 style={{ fontSize: "0.85rem", margin: "0 0 0.3rem" }}>
            {labelForKind(kind)} ({rows.length})
          </h3>
          <ul
            style={{
              listStyle: "none",
              padding: 0,
              margin: 0,
              display: "flex",
              flexDirection: "column",
              gap: "0.25rem",
            }}
          >
            {rows.map((r) => (
              <li
                key={`${r.entity_kind}-${r.entity_id}`}
                style={{
                  padding: "0.4rem 0.6rem",
                  border: "1px solid var(--bv-card-border, #e5e7eb)",
                  borderRadius: 6,
                  background: changeBg(r.change),
                  fontSize: "0.83rem",
                }}
              >
                <span
                  style={{
                    display: "inline-block",
                    minWidth: "5.5rem",
                    fontWeight: 600,
                  }}
                >
                  {labelForChange(r.change)}
                </span>
                <code style={{ fontSize: "0.7rem" }}>{r.entity_id.slice(0, 8)}</code>
                {advanced && (
                  <div
                    style={{
                      fontFamily: "monospace",
                      fontSize: "0.65rem",
                      color: "#6b7280",
                      marginTop: "0.2rem",
                    }}
                  >
                    {r.hash_a ? r.hash_a.slice(0, 12) : "—"}
                    {" → "}
                    {r.hash_b ? r.hash_b.slice(0, 12) : "—"}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function changeBg(c: "added" | "removed" | "modified"): string {
  if (c === "added") return "rgba(34, 197, 94, 0.08)";
  if (c === "removed") return "rgba(239, 68, 68, 0.08)";
  return "rgba(59, 130, 246, 0.08)";
}
