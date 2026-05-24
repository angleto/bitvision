"use client";

// Per-patient calendar tab. Renders <EventCalendar> bound to a single
// patient. The shared site chrome (sidebar / breadcrumbs) lives in
// the parent layout; this page is intentionally thin.

import { useParams, useSearchParams } from "next/navigation";

import EventCalendar from "@/components/EventCalendar";
import type { CalendarView } from "@/components/calendar/CalendarToolbar";

export default function PatientCalendarPage() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const v = search.get("view");
  const initialView: CalendarView =
    v === "day" || v === "week" || v === "month" || v === "agenda" ? v : "week";

  if (!params.id) return null;

  return (
    <main style={{ padding: "1rem" }}>
      <EventCalendar
        patientId={params.id}
        initialView={initialView}
        // Until an explicit owner-check is wired here, allow actions
        // for any user that can see this page. The backend still gates
        // writes via WRITE_REPORT/can_patient; the UI flag only hides
        // the buttons.
        isOwner
      />
    </main>
  );
}
