"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError } from "@/lib/api";
import {
  type ProvenanceActivity,
  type ProvenanceEvent,
  type ProvenanceTargetKind,
  provenanceApi,
} from "@/lib/api_records";

interface Props {
  targetKind: ProvenanceTargetKind;
  targetId: string;
  limit?: number;
}

const ACTIVITY_COLOR: Record<ProvenanceActivity, string> = {
  create: "#059669",
  classify: "#0891b2",
  extract: "#2563eb",
  endorse: "#16a34a",
  sign: "#1d4ed8",
  reject: "#dc2626",
  supersede: "#d97706",
  merge: "#7c3aed",
  split: "#a855f7",
  cite: "#0ea5e9",
  link: "#0891b2",
  unlink: "#9ca3af",
  redact: "#6b7280",
  delete: "#dc2626",
  restore: "#059669",
  identify: "#a16207",
  update: "#374151",
};

export default function ProvenanceTimeline({ targetKind, targetId, limit = 100 }: Props) {
  const t = useTranslations("provenanceTimeline");
  const tActivity = useTranslations("provenanceTimeline.activity");
  const [events, setEvents] = useState<ProvenanceEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEvents(null);
    setError(null);
    provenanceApi
      .read(targetKind, targetId, { limit })
      .then((rows) => {
        if (!cancelled) setEvents(rows);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? `${e.status}: ${e.message}` : t("errorGeneric"));
      });
    return () => {
      cancelled = true;
    };
  }, [targetKind, targetId, limit, t]);

  if (error) {
    return (
      <p role="alert" style={{ color: "#c00" }}>
        {error}
      </p>
    );
  }
  if (events === null) {
    return <p>{t("loading")}</p>;
  }
  if (events.length === 0) {
    return <p style={{ color: "var(--muted-fg, #666)" }}>{t("empty")}</p>;
  }

  return (
    <ol className="provenance-timeline" style={{ listStyle: "none", padding: 0 }}>
      {events.map((ev) => {
        const detail = ev.agent_subject_id
          ? t("subjectSuffix", { id: ev.agent_subject_id })
          : ev.agent_token_id
            ? t("tokenSuffix", { id: ev.agent_token_id })
            : "";
        const agentLabel =
          ev.agent_kind === "agent"
            ? t("agentLabel")
            : ev.agent_kind === "human"
              ? t("humanLabel")
              : t("systemLabel");
        return (
          <li
            key={ev.id}
            style={{
              display: "grid",
              gridTemplateColumns: "auto 1fr",
              gap: "0.75rem",
              padding: "0.5rem 0",
              borderBottom: "1px solid var(--border, #e5e7eb)",
            }}
          >
            <span
              style={{
                padding: "0.25rem 0.625rem",
                borderRadius: "999px",
                background: ACTIVITY_COLOR[ev.activity],
                color: "white",
                fontSize: "0.75rem",
                alignSelf: "start",
                minWidth: "5rem",
                textAlign: "center",
              }}
            >
              {(() => {
                try {
                  return tActivity(ev.activity);
                } catch {
                  return ev.activity;
                }
              })()}
            </span>
            <div>
              <div
                style={{
                  display: "flex",
                  gap: "0.5rem",
                  alignItems: "baseline",
                }}
              >
                <span
                  title={t("agentTitle", { kind: ev.agent_kind, detail })}
                  style={{
                    fontSize: "0.75rem",
                    padding: "0.125rem 0.5rem",
                    borderRadius: "0.25rem",
                    background:
                      ev.agent_kind === "human"
                        ? "#dcfce7"
                        : ev.agent_kind === "agent"
                          ? "#fef3c7"
                          : "#e5e7eb",
                    color:
                      ev.agent_kind === "human"
                        ? "#166534"
                        : ev.agent_kind === "agent"
                          ? "#92400e"
                          : "#374151",
                  }}
                >
                  {agentLabel}
                </span>
                <small style={{ color: "var(--muted-fg, #666)" }}>
                  {new Date(ev.recorded_at).toLocaleString()}
                </small>
              </div>

              {ev.source_kind && ev.source_id && (
                <div style={{ fontSize: "0.875rem", marginTop: "0.25rem" }}>
                  {t("fromLabel")}{" "}
                  <Link href={`/provenance/${ev.source_kind}/${ev.source_id}`}>
                    <code>
                      {ev.source_kind}:{ev.source_id.slice(0, 8)}…
                    </code>
                  </Link>
                </div>
              )}

              {ev.diff && Object.keys(ev.diff).length > 0 && (
                <details style={{ marginTop: "0.25rem" }}>
                  <summary style={{ cursor: "pointer", fontSize: "0.875rem" }}>
                    {t("details")}
                  </summary>
                  <pre
                    style={{
                      background: "var(--muted-bg, #f3f4f6)",
                      padding: "0.5rem",
                      borderRadius: "0.25rem",
                      fontSize: "0.8em",
                      overflowX: "auto",
                    }}
                  >
                    {JSON.stringify(ev.diff, null, 2)}
                  </pre>
                </details>
              )}

              {ev.signature_hash && (
                <small
                  style={{
                    fontFamily: "var(--font-mono, monospace)",
                    fontSize: "0.7em",
                    color: "var(--muted-fg, #999)",
                    display: "block",
                    marginTop: "0.25rem",
                  }}
                >
                  hash: {ev.signature_hash.slice(0, 16)}…
                </small>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
