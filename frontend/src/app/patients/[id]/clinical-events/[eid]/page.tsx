"use client";

// Canonical route for a clinical event viewed inside a patient's Health
// Record. The patient id sits in the URL so cross-patient access is
// structurally inexpressible: a request for
// ``/patients/A/clinical-events/<eid-of-B>`` is rejected before
// ``ClinicalEventContent`` ever sees the data.
//
// The legacy ``/clinical-events/:id`` route still exists as a backward-
// compat redirect that resolves the event's ``patient_id`` and bounces
// here, so existing bookmarks and ``?from=event`` back-links continue
// to work.

import { useTranslations } from "next-intl";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import ClinicalEventContent from "@/components/ClinicalEventContent";
import { ApiError } from "@/lib/api";
import { type ClinicalEvent, clinicalEventsApi } from "@/lib/api_records";

type GuardState =
  | { kind: "loading" }
  | { kind: "ok"; event: ClinicalEvent }
  // ``hidden`` lumps together "event does not exist" and "event exists
  // but belongs to a different patient" — surfacing the distinction
  // would let an attacker enumerate event ids of an unrelated patient.
  | { kind: "hidden" }
  | { kind: "error" };

export default function PatientClinicalEventPage() {
  const params = useParams<{ id: string; eid: string }>();
  const t = useTranslations("clinicalEventDetail");
  const [state, setState] = useState<GuardState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    clinicalEventsApi
      .read(params.eid)
      .then((event) => {
        if (cancelled) return;
        if (event.patient_id !== params.id) {
          setState({ kind: "hidden" });
          return;
        }
        setState({ kind: "ok", event });
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
  }, [params.eid, params.id]);

  if (state.kind === "loading") {
    return (
      <main>
        <p className="meta">{t("loading")}</p>
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
        <p className="error">{t("errorLoad")}</p>
      </main>
    );
  }
  return <ClinicalEventContent eventId={params.eid} initialEvent={state.event} />;
}
