"use client";

import Link from "next/link";

import type { Consultation, ConsultationStatus } from "@/lib/api";

interface Props {
  patientId: string;
  consultation: Consultation;
}

export const STATUS_STYLES: Record<
  ConsultationStatus,
  { bg: string; color: string; label: string }
> = {
  draft: { bg: "#f3f4f6", color: "#374151", label: "bozza" },
  submitted: { bg: "#dbeafe", color: "#1e40af", label: "inviato" },
  signed: { bg: "#dcfce7", color: "#166534", label: "firmato" },
  rejected: { bg: "#fee2e2", color: "#991b1b", label: "rifiutato" },
};

export function ConsultationBadges({ c }: { c: Consultation }) {
  const st = STATUS_STYLES[c.status];
  return (
    <div className="badges">
      <span className="badge" style={{ background: st.bg, color: st.color }}>
        {st.label}
      </span>
      <span className="badge" style={{ marginLeft: "0.3rem" }}>
        {c.author_kind}
      </span>
      {c.model_id && (
        <span className="badge" style={{ marginLeft: "0.3rem" }}>
          {c.model_id}
        </span>
      )}
      {c.de_identified && (
        <span
          className="badge"
          style={{ marginLeft: "0.3rem", background: "#fef3c7", color: "#92400e" }}
        >
          de-identified
        </span>
      )}
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n)}...`;
}

export default function ConsultationCard({ patientId, consultation }: Props) {
  return (
    <Link
      href={`/patients/${patientId}/consultations/${consultation.id}`}
      className="card"
      style={{ display: "block", color: "inherit", marginBottom: "0.5rem" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>{consultation.title}</h3>
        <span className="meta" style={{ fontSize: "0.8rem" }}>
          {new Date(consultation.created_at).toLocaleString()}
        </span>
      </div>
      <div style={{ marginTop: "0.4rem" }}>
        <ConsultationBadges c={consultation} />
      </div>
      {consultation.summary_md && (
        <p className="meta" style={{ marginTop: "0.4rem" }}>
          {truncate(consultation.summary_md, 200)}
        </p>
      )}
    </Link>
  );
}
