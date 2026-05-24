"use client";

// Canonical route for a study viewed inside a patient's Health Record.
// The patient id sits in the URL so cross-patient access is structurally
// inexpressible: a request for ``/patients/A/studies/<sid-of-B>`` is
// rejected here before ``StudyDetailContent`` ever sees the data.
//
// The legacy ``/studies/:id`` route still exists as a backward-compat
// redirect (it resolves the study's ``patient_id`` and bounces here),
// so any external link or bookmark that pre-dates this change keeps
// working.

import { useTranslations } from "next-intl";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import StudyDetailContent from "@/components/StudyDetailContent";
import { ApiError, type StudyDetail, studiesApi } from "@/lib/api";

type GuardState =
  | { kind: "loading" }
  | { kind: "ok"; study: StudyDetail }
  // ``hidden`` lumps together "study does not exist" and "study exists
  // but belongs to a different patient" — surfacing the distinction
  // would let an attacker probing patient A enumerate study ids of
  // patient B. The user-facing message is identical for both cases.
  | { kind: "hidden" }
  | { kind: "error" };

export default function PatientStudyPage() {
  const params = useParams<{ id: string; sid: string }>();
  const t = useTranslations("study");
  const [state, setState] = useState<GuardState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    studiesApi
      .detail(params.sid)
      .then((study) => {
        if (cancelled) return;
        // ``patient_id`` is nullable for legacy orphan studies. We
        // tolerate the orphan case (no patient bound) when the URL is
        // the legacy redirect path; here we are explicitly inside a
        // patient namespace, so an unset/mismatched ``patient_id`` is
        // a guard violation.
        if (!study.patient_id || study.patient_id !== params.id) {
          setState({ kind: "hidden" });
          return;
        }
        setState({ kind: "ok", study });
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
          setState({ kind: "hidden" });
          return;
        }
        setState({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [params.sid, params.id]);

  if (state.kind === "loading") {
    return (
      <main>
        <p className="meta">…</p>
      </main>
    );
  }
  if (state.kind === "hidden") {
    return (
      <main>
        <p className="error">{t("crossPatient")}</p>
      </main>
    );
  }
  if (state.kind === "error") {
    return (
      <main>
        <p className="error">{t("loadFailed")}</p>
      </main>
    );
  }
  return <StudyDetailContent studyId={params.sid} initialStudy={state.study} />;
}
