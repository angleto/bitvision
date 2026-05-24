"use client";

// Global calendar aggregator. Lists upcoming events across all
// patients the current user can see. Cross-patient is NOT a SQL join
// on the backend (would violate the patient-namespacing invariant);
// instead the page calls /api/patients/{pid}/calendar once per
// accessible patient and stitches the results client-side.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import EventDrawer from "@/components/EventDrawer";
import PlanEventDialog from "@/components/PlanEventDialog";
import CalendarAgenda from "@/components/calendar/CalendarAgenda";
import { ApiError, type Patient, patientsApi } from "@/lib/api";
import type { CalendarOccurrence, EventStatus } from "@/lib/api_records";
import { calendarApi } from "@/lib/calendar_api";

interface OccWithPatient extends CalendarOccurrence {
  _patientId: string;
}

const VISIBLE_STATUSES: EventStatus[] = ["planned", "confirmed"];

export default function GlobalCalendarPage() {
  const t = useTranslations("calendar");
  const [patients, setPatients] = useState<Patient[]>([]);
  const [occurrences, setOccurrences] = useState<OccWithPatient[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedPatient, setSelectedPatient] = useState<Patient | null>(null);
  const [selected, setSelected] = useState<CalendarOccurrence | null>(null);
  const [planForPatient, setPlanForPatient] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  // Step 1: fetch the visible patient list (capped at 200 for the
  // aggregator; if the user manages more they should use per-patient
  // calendars from the patient page).
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    patientsApi
      .list({ limit: 200 })
      .then((page) => {
        if (cancelled) return;
        setPatients(page.items ?? []);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Step 2: once patients are known, fetch each calendar feed in
  // parallel and merge. The from/to range is +90d / -7d from today
  // (the global view emphasises the immediate horizon).
  // biome-ignore lint/correctness/useExhaustiveDependencies: VISIBLE_STATUSES is a module-level constant; biome cannot see it but its value never changes.
  useEffect(() => {
    let cancelled = false;
    if (patients.length === 0) {
      setLoading(false);
      return;
    }
    const today = new Date();
    const from = isoDate(addDays(today, -7));
    const to = isoDate(addDays(today, 90));
    setLoading(true);
    Promise.all(
      patients.map((p) =>
        calendarApi
          .feed(p.id, { from, to, statuses: VISIBLE_STATUSES })
          .then((f) => f.occurrences.map((occ): OccWithPatient => ({ ...occ, _patientId: p.id })))
          .catch(() => [] as OccWithPatient[]),
      ),
    )
      .then((arrs) => {
        if (cancelled) return;
        setOccurrences(arrs.flat());
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [patients, reloadTick]);

  const labelByPatient = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of patients) m.set(p.id, p.display_name ?? p.id);
    return m;
  }, [patients]);

  const labelFor = useCallback(
    (occ: CalendarOccurrence): string | undefined => {
      const withPatient = occ as OccWithPatient;
      return labelByPatient.get(withPatient._patientId);
    },
    [labelByPatient],
  );

  return (
    <main style={{ padding: "1rem" }}>
      <header style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
        <h1 style={{ margin: 0, fontSize: "1.4rem" }}>{t("globalTitle")}</h1>
        <span style={{ flex: 1 }} />
        <select
          aria-label={t("planForPatient")}
          value={selectedPatient?.id ?? ""}
          onChange={(e) => {
            const p = patients.find((pt) => pt.id === e.target.value) ?? null;
            setSelectedPatient(p);
            if (p) setPlanForPatient(p.id);
          }}
          style={{ fontSize: "0.85rem", padding: "0.3rem" }}
        >
          <option value="">+ {t("planEvent")}…</option>
          {patients.map((p) => (
            <option key={p.id} value={p.id}>
              {p.display_name ?? p.id}
            </option>
          ))}
        </select>
      </header>
      {error && (
        <p role="alert" style={{ color: "var(--bv-danger, #c00)" }}>
          {error}
        </p>
      )}
      {loading && <p style={{ color: "var(--bv-fg-soft)" }}>…</p>}
      {!loading && !error && (
        <CalendarAgenda
          occurrences={occurrences}
          onSelect={setSelected}
          patientLabelFor={labelFor}
        />
      )}
      {selected && (
        <EventDrawer
          eventId={selected.event_id}
          isOwner
          onClose={() => setSelected(null)}
          onChanged={() => {
            setSelected(null);
            setReloadTick((x) => x + 1);
          }}
        />
      )}
      {planForPatient && (
        <PlanEventDialog
          patientId={planForPatient}
          onClose={() => {
            setPlanForPatient(null);
            setSelectedPatient(null);
          }}
          onCreated={() => {
            setPlanForPatient(null);
            setSelectedPatient(null);
            setReloadTick((x) => x + 1);
          }}
        />
      )}
    </main>
  );
}

function addDays(d: Date, days: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + days);
  return x;
}

function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
