"use client";

// Tag detail page — lists every clinical note (Evidenze e sintesi
// entry) of the active patient whose body mentions ``#<value>``.
// Filtering is patient-scoped on the backend via ``listNotes``; the
// ``#tag`` substring match runs client-side as a cheap "good enough"
// filter that doesn't require a dedicated tag → resources endpoint.
//
// Cross-patient guarantees: ``listNotes`` is already scoped to the
// active patient (URL param), so even if the tag value collides with
// a tag used on a different patient, no foreign data leaks here.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import EvidenceContent from "@/components/EvidenceContent";
import { ApiError, type ClinicalNote, type Patient, patientsApi } from "@/lib/api";

export default function PatientTagPage() {
  const params = useParams<{ id: string; value: string }>();
  const search = useSearchParams();
  const tEv = useTranslations("evidence");
  const tFasc = useTranslations("fascicolo");
  const tUi = useTranslations("uiCommon");
  const patientId = params.id;
  const tagValue = decodeURIComponent(params.value);
  const ctx = search.get("ctx");

  const [patient, setPatient] = useState<Patient | null>(null);
  const [notes, setNotes] = useState<ClinicalNote[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!patientId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr(null);
      try {
        const [p, n] = await Promise.all([
          patientsApi.detail(patientId),
          patientsApi.listNotes(patientId),
        ]);
        if (cancelled) return;
        setPatient(p);
        // Substring match on ``#<value>``. Case-sensitive to mirror
        // the parser regex; the user types the same casing they
        // intend the chip to point at.
        const needle = `#${tagValue}`;
        setNotes(n.filter((x) => x.body.includes(needle)));
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [patientId, tagValue]);

  return (
    <main>
      <p className="meta">
        {ctx?.startsWith("evidence") ? (
          <Link href={`/patients/${patientId}`}>← {tEv("backToEvidence")}</Link>
        ) : (
          <Link href={`/patients/${patientId}`}>← {tFasc("treeRootLabel")}</Link>
        )}
      </p>
      <h1
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "0.5rem",
        }}
      >
        <span
          style={{
            background: "var(--bv-warning-soft, #fef3c7)",
            color: "var(--bv-warning, #b45309)",
            borderRadius: 999,
            padding: "2px 12px",
            fontSize: "1rem",
            fontWeight: 500,
          }}
        >
          #{tagValue}
        </span>
      </h1>
      {patient && (
        <p className="meta" style={{ marginTop: "-0.4rem" }}>
          {patient.display_name}
        </p>
      )}
      {loading && <p className="meta">{tUi("loading")}</p>}
      {err && !loading && <p className="error">{err}</p>}
      {!loading && !err && notes.length === 0 && (
        <p className="meta">{tEv("tagPage.empty", { value: tagValue })}</p>
      )}
      {!loading && !err && notes.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.85rem" }}>
          {notes.map((note) => (
            <article key={note.id} className="card" style={{ padding: "1rem 1.25rem" }}>
              <div
                className="meta"
                style={{
                  fontSize: "0.8rem",
                  marginBottom: "0.4rem",
                }}
              >
                {new Date(note.created_at).toLocaleString(undefined, {
                  day: "2-digit",
                  month: "short",
                  year: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </div>
              <EvidenceContent
                patientId={patientId}
                body={note.body}
                ctx={`evidence:tag:${tagValue}`}
              />
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
