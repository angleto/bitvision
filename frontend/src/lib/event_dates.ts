// Single source of truth for "when did / will this clinical event happen?".
//
// Mirrors the backend rule (see ``bvphoenix.api.clinical_events`` and the
// ``fn_ce_derive_event_date`` trigger installed by migration 0047):
// ``event_date`` is DERIVED from the anchor timestamp that matches the row's
// status family, in the row's timezone. Two families exist:
//
//   planned family : planned | confirmed | rescheduled | cancelled
//                    → anchored on ``planned_start_at``
//   actual  family : completed | missed
//                    → anchored on ``actual_start_at``
//
// A row whose anchor is NULL (DICOM StudyDate imports, document backfills)
// keeps a standalone ``event_date``, and that is the ONLY case where the
// date may be written directly.
//
// Every FE surface that displays, sorts or edits an event date goes through
// here, so a divergence from the DB trigger is a one-file fix rather than a
// hunt through dialogs and timelines.

export type AnchorFamily = "planned" | "actual";

/** Which family a status belongs to. Kept as a plain string list rather than
 *  the ``EventStatus`` union so a status added server-side before the FE
 *  types catch up degrades to the ``actual`` family (the backend default for
 *  a row with no explicit status) instead of a type error. */
const PLANNED_ANCHOR_STATUSES: readonly string[] = [
  "planned",
  "confirmed",
  "rescheduled",
  "cancelled",
];

/** The temporal shape of a clinical event, structurally typed so both
 *  ``ClinicalEvent`` (detail payload) and ``TimelineEvent`` (timeline
 *  payload) satisfy it without a cast. */
export interface EventTimeFields {
  event_status?: string | null;
  planned_start_at?: string | null;
  planned_end_at?: string | null;
  actual_start_at?: string | null;
  actual_end_at?: string | null;
  event_date?: string | null;
  timezone?: string | null;
}

/** ``YYYY-MM-DD``: a DATE column, not an instant. ``Date.parse`` reads this
 *  form as UTC midnight, which silently shifts the day for viewers west of
 *  Greenwich, so every code path below branches on it. */
const DATE_ONLY_RE = /^\d{4}-\d{2}-\d{2}$/;

export function anchorFamily(status: string | null | undefined): AnchorFamily {
  // The backend column defaults to 'completed', so an absent status is an
  // actual-family row, not a planned one.
  return PLANNED_ANCHOR_STATUSES.includes(status ?? "completed") ? "planned" : "actual";
}

/** The instant the row's ``event_date`` is derived from, or null when the
 *  row is date-only. */
export function authoritativeInstant(event: EventTimeFields): string | null {
  const iso =
    anchorFamily(event.event_status) === "planned" ? event.planned_start_at : event.actual_start_at;
  return iso ?? null;
}

/** The end of the anchor interval matching the row's family. */
export function authoritativeEnd(event: EventTimeFields): string | null {
  const iso =
    anchorFamily(event.event_status) === "planned" ? event.planned_end_at : event.actual_end_at;
  return iso ?? null;
}

/** True when the row has no anchor: ``event_date`` stands alone and is the
 *  only writable temporal field. */
export function isDateOnly(event: EventTimeFields): boolean {
  return authoritativeInstant(event) === null;
}

/** What the displayed date actually comes from. Drives the caption that
 *  makes the derivation visible instead of leaving the user to guess. */
export function dateSource(event: EventTimeFields): "planned" | "actual" | "dateOnly" {
  if (isDateOnly(event)) return "dateOnly";
  return anchorFamily(event.event_status);
}

/** What a read-only surface should SAY about the date it is showing.
 *
 *  Finer than ``dateSource`` on purpose: a row with no anchor is not
 *  automatically a date-only import. ``mark-missed`` never writes
 *  ``actual_start_at``, so a missed appointment lands in the actual family
 *  with a NULL anchor while still carrying the ``planned_start_at`` it was
 *  booked for. Captioning that "date only, no time recorded" is false: the
 *  booked time is right there in the row. ``missingAnchor`` is that case;
 *  ``dateOnly`` stays reserved for a row that genuinely never had a time
 *  (DICOM StudyDate imports, document backfills).
 *
 *  Built on ``dateSource`` rather than repeating the family rule, and the
 *  ``missingAnchor`` case is exactly "``secondaryPlannedInstant`` has
 *  something to show", so a caller can caption the row and render the booked
 *  time off the same condition. */
export function whenSource(
  event: EventTimeFields,
): "planned" | "actual" | "missingAnchor" | "dateOnly" {
  const src = dateSource(event);
  if (src !== "dateOnly") return src;
  return secondaryPlannedInstant(event) !== null ? "missingAnchor" : "dateOnly";
}

/** The caption a surface shows under a date, keyed by ``whenSource``.
 *
 *  Values are keys inside the ``eventActions`` namespace, i.e. the caption
 *  half of ``whenSource`` itself. It lives here rather than in each
 *  component because the drawer and the edit dialog sit one click apart: two
 *  copies of this table meant the same row could be captioned "no time
 *  recorded" in one and "booked time" in the other. */
export const WHEN_CAPTION = {
  planned: "dateSourcePlanned",
  actual: "dateSourceActual",
  missingAnchor: "dateSourceMissingAnchor",
  dateOnly: "dateSourceDateOnly",
} as const;

export type WhenCaptionKey = (typeof WHEN_CAPTION)[keyof typeof WHEN_CAPTION];

/** ``whenSource`` + ``WHEN_CAPTION`` in one call, for the common case where
 *  a surface only wants the message key. */
export function whenCaptionKey(event: EventTimeFields): WhenCaptionKey {
  return WHEN_CAPTION[whenSource(event)];
}

/** The non-authoritative scheduled time, when the row carries one that is
 *  not already the instant the date derives from.
 *
 *  Covers both a completed visit (booked Tuesday 09:00, actually done
 *  Thursday) and a missed one (booked Tuesday 09:00, no actual time ever
 *  written). Returns null for a planned-family row, where the planned start
 *  IS the authoritative instant and repeating it would be noise. */
export function secondaryPlannedInstant(event: EventTimeFields): string | null {
  const planned = event.planned_start_at ?? null;
  if (!planned) return null;
  return sameInstant(planned, authoritativeInstant(event)) ? null : planned;
}

// ---------------------------------------------------------------------------
// Timezone
// ---------------------------------------------------------------------------

/** The shape of an IANA name: ``Europe/Rome``, ``UTC``, ``Etc/GMT+1``.
 *
 *  ``Intl.DateTimeFormat`` alone is too permissive for this field. Some
 *  engines accept a bare UTC offset (``+01:00``) that the backend's
 *  ``ZoneInfo`` then rejects with 422 ``invalid_timezone``, which is exactly
 *  the round-trip this check exists to avoid. */
const IANA_SHAPE = /^[A-Za-z][A-Za-z0-9_+-]*(?:\/[A-Za-z0-9_+-]+)*$/;

/** True when ``tz`` is a zone name this browser (and, by construction, the
 *  server's tz database) can resolve. An empty value is NOT valid here:
 *  callers decide whether "unset" is acceptable for their field, and both
 *  dialogs treat an empty timezone box as "leave it to the server". */
export function isValidTimezone(tz: string | null | undefined): boolean {
  if (!tz || !IANA_SHAPE.test(tz)) return false;
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

/** Every zone this engine knows, for a ``<datalist>``. Empty when the engine
 *  predates ``Intl.supportedValuesOf`` (Safari < 15.4): the input stays free
 *  text there, still guarded by ``isValidTimezone``. */
export function timezoneOptions(): readonly string[] {
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf;
    return typeof supported === "function" ? supported.call(Intl, "timeZone") : [];
  } catch {
    return [];
  }
}

/** The zone this browser is set to, i.e. the one every
 *  ``<input type="datetime-local">`` on the page silently speaks. "UTC" when
 *  the engine cannot say. */
export function viewerTimezone(): string {
  if (typeof Intl === "undefined") return "UTC";
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** Minutes ``tz`` is ahead of UTC at instant ``at``, or null when the zone
 *  is unusable. Computed by reading the wall clock the zone shows and
 *  subtracting the instant, so DST is handled by the tz database rather
 *  than by a table here. */
function zoneOffsetMinutes(tz: string, at: number): number | null {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).formatToParts(new Date(at));
    const get = (type: string): number => Number(parts.find((p) => p.type === type)?.value);
    const [y, mo, d, h, mi, s] = ["year", "month", "day", "hour", "minute", "second"].map(get);
    if ([y, mo, d, h, mi, s].some((n) => Number.isNaN(n))) return null;
    // ``hour12: false`` renders midnight as 24 on some engines.
    const wall = Date.UTC(y, mo - 1, d, h % 24, mi, s);
    if (Number.isNaN(wall)) return null;
    // ``at`` may carry milliseconds the wall clock does not: offsets are
    // whole minutes, so rounding recovers the exact value.
    return Math.round((wall - at) / 60_000);
  } catch {
    return null;
  }
}

/** The viewer's zone name when a ``<input type="datetime-local">`` in this
 *  form would show a DIFFERENT wall clock than everything the form renders
 *  in the event's own zone, null when the two agree.
 *
 *  The confusion this exists to kill: the input element is hard-wired to the
 *  device's zone (it has no timeZone option and never will), while the
 *  caption, the "originally planned" line and the recorded-date preview all
 *  go through ``formatInZone`` with the event's zone. A viewer in London
 *  editing a 09:00 Europe/Rome appointment therefore sees 08:00 in the box
 *  and 09:00 two lines below, both correct, neither labelled.
 *
 *  We name the zones instead of converting the input's value: marshalling
 *  the box through the event's zone would mean writing a wall clock →
 *  instant conversion that has to pick a side on DST gaps and repeats, and
 *  ``validateAmend``, ``max={nowLocalInputValue()}`` and
 *  ``fromLocalInputValue`` would all need the same conversion to stay
 *  consistent. Every one of those is a chance to store an anchor an hour off
 *  the truth. Naming the zone cannot move an instant.
 *
 *  Returns null when the event has no usable zone: ``formatInZone`` then
 *  falls back to the viewer's zone too, so nothing on screen disagrees.
 *  Compares OFFSETS, not names, so Europe/Rome vs Europe/Vatican (identical
 *  clocks) stays quiet, and the comparison is made at the instant being
 *  edited so a summer appointment edited in winter is judged on its own
 *  DST state. */
export function localInputZoneMismatch(
  eventTz: string | null | undefined,
  at: number = Date.now(),
  viewerTz: string = viewerTimezone(),
): string | null {
  if (!eventTz || !isValidTimezone(eventTz)) return null;
  if (!isValidTimezone(viewerTz)) return null;
  const eventOffset = zoneOffsetMinutes(eventTz, at);
  const viewerOffset = zoneOffsetMinutes(viewerTz, at);
  if (eventOffset === null || viewerOffset === null) return null;
  return eventOffset === viewerOffset ? null : viewerTz;
}

/** The calendar date the DB trigger will derive for this anchor + zone.
 *
 *  Same projection as ``fn_ce_derive_event_date`` (``AT TIME ZONE tz``, UTC
 *  when unset), computed in the browser so the form can show the date that
 *  will actually be recorded before anything is sent. A date-only value is
 *  already a date and passes through untouched. Returns "" for an empty or
 *  unparseable input. */
export function derivedEventDate(iso: string | null | undefined, tz?: string | null): string {
  if (!iso) return "";
  if (DATE_ONLY_RE.test(iso)) return iso;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const zone = tz && isValidTimezone(tz) ? tz : "UTC";
  // ``formatToParts`` rather than an ``en-CA`` string: the numeric parts are
  // specified, the separator an engine picks for a locale is not.
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const get = (type: string): string => parts.find((p) => p.type === type)?.value ?? "";
  const [y, mo, day] = [get("year"), get("month"), get("day")];
  return y && mo && day ? `${y}-${mo}-${day}` : "";
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

export type AmendErrorKey = "amendEndBeforeStart" | "amendFutureOnPast" | "amendInvalidDate";
export type AmendWarningKey = "amendPastOnPlanned";

export interface AmendValidationInput {
  family: AnchorFamily;
  /** ISO instant, ``<input type="datetime-local">`` value, or ``YYYY-MM-DD``. */
  start?: string | null;
  end?: string | null;
  /** Epoch ms; injectable so the rules are testable without faking clocks. */
  now?: number;
}

export interface AmendValidationResult {
  errorKey?: AmendErrorKey;
  warningKey?: AmendWarningKey;
}

/** The instants a temporal value spans. A timed value is a point (lo === hi);
 *  a date-only value spans its whole local day, which is what keeps "today"
 *  from reading as both past and future. */
function bounds(value: string | null | undefined): { lo: number; hi: number } | null {
  if (!value) return null;
  if (DATE_ONLY_RE.test(value)) {
    const [y, mo, d] = value.split("-").map(Number);
    const lo = new Date(y, mo - 1, d, 0, 0, 0, 0).getTime();
    if (Number.isNaN(lo)) return null;
    return { lo, hi: lo + 86_400_000 - 1 };
  }
  const t = Date.parse(value);
  if (Number.isNaN(t)) return null;
  return { lo: t, hi: t };
}

/** Mirrors the 422 codes the amend-time endpoint raises, in the same order,
 *  so the user is stopped by the form rather than by the server.
 *
 *  A planned event dated in the past is NOT an error: backfilling a schedule
 *  that was agreed weeks ago is legitimate, so it only earns a warning. */
export function validateAmend({
  family,
  start,
  end,
  now = Date.now(),
}: AmendValidationInput): AmendValidationResult {
  const s = bounds(start);
  const e = bounds(end);
  if (start && s === null) return { errorKey: "amendInvalidDate" };
  if (end && e === null) return { errorKey: "amendInvalidDate" };
  if (s && e && e.lo < s.lo) return { errorKey: "amendEndBeforeStart" };
  if (family === "actual" && s && s.lo > now) return { errorKey: "amendFutureOnPast" };
  if (family === "planned" && s && s.hi < now) return { warningKey: "amendPastOnPlanned" };
  return {};
}

/** Backend rule: correcting a REALISED clinical fact (the actual_* family, or
 *  the standalone date of a date-only historical row) is a record amendment
 *  and must say why. Correcting a plan that has not happened yet is ordinary
 *  editing and needs no justification. */
export function reasonRequired(family: AnchorFamily, editingEventDate: boolean): boolean {
  return family === "actual" || editingEventDate;
}

// ---------------------------------------------------------------------------
// Formatting / input marshalling
// ---------------------------------------------------------------------------

/** Render an instant (or a date-only value) in the event's own timezone, so
 *  a scan booked for 09:00 in Rome never reads as 08:00 because the viewer
 *  happens to be in London. Falls back to the viewer's zone when the row
 *  carries an unusable IANA name (legacy imports do). */
export function formatInZone(
  iso: string | null | undefined,
  locale: string,
  tz?: string | null,
): string {
  if (!iso) return "";
  if (DATE_ONLY_RE.test(iso)) {
    // A DATE has no instant: pin it to UTC so no zone can move the day.
    const d = new Date(`${iso}T00:00:00Z`);
    if (Number.isNaN(d.getTime())) return iso;
    return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" }).format(d);
  }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const opts: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "short" };
  try {
    return new Intl.DateTimeFormat(locale, { ...opts, timeZone: tz || undefined }).format(d);
  } catch {
    return new Intl.DateTimeFormat(locale, opts).format(d);
  }
}

/** ISO instant → the ``yyyy-MM-ddTHH:mm`` local wall-clock string that
 *  ``<input type="datetime-local">`` requires. */
export function toLocalInputValue(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** ``<input type="datetime-local">`` value → ISO instant, or null when empty
 *  or unparseable. The input value is local wall-clock time, which
 *  ``new Date(...)`` reads correctly for the date-time form. */
export function fromLocalInputValue(value: string | null | undefined): string | null {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

/** Today, as the ``YYYY-MM-DD`` an ``<input type="date">`` speaks. */
export function todayLocalDate(now: number = Date.now()): string {
  const d = new Date(now);
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Now, as a ``<input type="datetime-local">`` ``max`` bound. */
export function nowLocalInputValue(now: number = Date.now()): string {
  return toLocalInputValue(new Date(now).toISOString());
}

/** Compare two temporal values by the instant they denote, not by their text.
 *
 *  This is the fix for the "the dialog re-sent planned_start_at on every
 *  save" bug: the server emits ``2026-08-21T10:00:00+00:00`` while
 *  ``Date.toISOString()`` emits ``2026-08-21T10:00:00.000Z``. Same moment,
 *  different strings, so a string comparison declared a change every time.
 *  Two absent values (null / undefined / "") count as equal. */
export function sameInstant(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return !a && !b;
  const ta = Date.parse(a);
  const tb = Date.parse(b);
  if (Number.isNaN(ta) || Number.isNaN(tb)) return a === b;
  return ta === tb;
}
