import type { PatientContact } from "@/lib/api";

/**
 * Contact payload helpers for the patient edit form.
 *
 * Why this is a module and not three lines inside the form
 * --------------------------------------------------------
 *
 * `PATCH /api/patients/{id}` takes the **whole** contacts array and
 * reconciles the table against it. The form used to build that array as
 * `{label, relationship, email, phone}` — dropping `id`. The backend
 * matched incoming entries on `id`, so every save inserted a brand-new
 * row for every contact, and the pre-existing rows that carried a
 * delegation were held back from deletion because dropping somebody's
 * access silently is worse than leaving a stale row. The result was one
 * duplicate per delegated contact per save, plus a silent reset of the
 * consent flags, notification channels and RFC 8058 opt-out token on
 * every contact that *was* replaced.
 *
 * Deleting a duplicate through the same form made it worse: the removed
 * row was gone, but every remaining row was re-inserted under a new id
 * while the delegated originals stayed. On a real fascicolo five
 * contacts became eight.
 *
 * The backend now also matches on the address and the database refuses
 * two contacts sharing a mailbox on one patient, so neither side can
 * produce that outcome alone. This module is the client half: the
 * identity goes out with the payload, and a unit test holds it there.
 */

/** A contact row while it is being edited, with a React key that is
 *  stable across reorders. `_uiKey` is local state and never travels. */
export type EditableContact = PatientContact & { _uiKey: string };

/** What the PATCH body carries per contact. `id` is the whole point. */
export interface ContactPayloadEntry {
  id: string | null;
  label: string;
  relationship: string | null;
  email: string | null;
  phone: string | null;
}

function trimmedOrNull(value: string | null | undefined): string | null {
  const v = (value ?? "").trim();
  return v.length > 0 ? v : null;
}

/**
 * Build the `contacts` array for `PATCH /api/patients/{id}`.
 *
 * Drops rows with no name (an empty row the user added and abandoned),
 * normalises blank strings to `null` so the backend stores `null`
 * consistently, and — the part that matters — carries `id` through for
 * every row that already has one, so the server updates instead of
 * inserting. A row the user just added has no `id` yet and sends `null`.
 */
export function buildContactPayload(contacts: readonly EditableContact[]): ContactPayloadEntry[] {
  return contacts
    .filter((c) => c.label.trim().length > 0)
    .map((c) => ({
      id: c.id ?? null,
      label: c.label.trim(),
      relationship: trimmedOrNull(c.relationship),
      email: trimmedOrNull(c.email),
      phone: trimmedOrNull(c.phone),
    }));
}

/**
 * Canonical form of an address, matching what the server stores.
 *
 * Mirrors `services.patient_contacts.normalise_email` and the
 * `trg_patient_contacts_normalise_email` trigger. Used to warn about a
 * collision before the request goes out, so the user sees which two rows
 * clash instead of a 409 about an address they have to go looking for.
 */
export function normaliseContactEmail(value: string | null | undefined): string | null {
  const v = (value ?? "").trim().toLowerCase();
  return v.length > 0 ? v : null;
}

/**
 * The first address used by more than one row, or `null` when the list
 * is clean. One contact per mailbox per patient is a database
 * constraint: the address is simultaneously the key a delegation
 * resolves the recipient's account by, the notification target, and the
 * identity behind the opt-out token.
 */
export function findDuplicateEmail(contacts: readonly EditableContact[]): string | null {
  const seen = new Set<string>();
  for (const c of contacts) {
    if (c.label.trim().length === 0) continue;
    const email = normaliseContactEmail(c.email);
    if (email === null) continue;
    if (seen.has(email)) return email;
    seen.add(email);
  }
  return null;
}
