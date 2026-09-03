# Security &amp; GDPR

Bitvision phoenix is designed for deployment in the European Union and
must comply with the General Data Protection Regulation (Regulation
(EU) 2016/679, "GDPR"). This document is the authoritative reference
for how the platform discharges those obligations. For the broader
security posture (auth, RLS, audit) see `authorization.md`. Section 9
covers the one store that lives on the data subject's own device rather
than on the server.

## 1. Data subject rights implemented

| GDPR Article | Right | Endpoint / mechanism |
|---|---|---|
| Art. 7(1) | Proof of consent | `consents` table, append-only history |
| Art. 13-15 | Right of access / information | `GET /api/gdpr/export` |
| Art. 16 | Right to rectification | `PATCH /api/patients/{id}`, `PATCH /api/auth/me` |
| Art. 17 | Right to erasure ("right to be forgotten") | `POST /api/gdpr/erasure-request` |
| Art. 18 | Right to restriction | via admin review flow |
| Art. 20 | Data portability | `GET /api/gdpr/export` (ZIP, JSON manifest) |
| Art. 21 | Right to object | revoke consent via `POST /api/gdpr/consent` |

## 2. Data model

### `consents` table

Append-only. One row per consent event. A consent is *active* when
`revoked_at IS NULL` and a matching `granted_at` row exists for the
(user, kind) pair. The history is preserved so a Data Protection
Officer (DPO) can always reconstruct exactly who consented to what and
when — required for Art. 7(1) burden of proof.

Columns:

- `id` UUID
- `user_subject_id` UUID (FK `subjects.id`, CASCADE)
- `kind` TEXT — see taxonomy in §3
- `granted_at` timestamptz (default now)
- `revoked_at` timestamptz (nullable)
- `metadata` JSONB — IP, user agent, locale, version of policy text shown, ...

### `data_erasure_requests` table

Tracks Art. 17 requests through a state machine:

`pending → approved → completed` (happy path)
`pending → rejected` (legal hold / ongoing investigation)
`pending → cancelled` (user withdrew)

Columns:

- `id` UUID
- `user_subject_id` UUID (FK `subjects.id`, CASCADE)
- `requested_at` timestamptz
- `completed_at` timestamptz (nullable)
- `reason` TEXT (user-supplied, free text)
- `scope` TEXT — see §4
- `status` VARCHAR(16) CHECK in ('pending','approved','rejected','completed','cancelled')
- `notes` JSONB — admin notes, legal hold references

## 3. Consent kinds taxonomy

Consents are identified by a stable string key. Keep this list in sync
with `bvphoenix.db.models.gdpr.CONSENT_KINDS` and with the
`CONSENT_LABELS` map in `frontend/src/app/settings/privacy/page.tsx`.

| Kind | Required | Description |
|---|---|---|
| `terms_of_service` | yes | Acceptance of the ToS; required to use the platform. |
| `privacy_policy` | yes | Acknowledgement of the privacy policy. |
| `marketing_email` | no | Product announcements, newsletters. |
| `research_use` | no | De-identified studies usable for academic research. |
| `commercial_use` | no | De-identified studies licensable commercially. |
| `ai_training` | no | De-identified studies usable for AI model training. |
| `third_party_sharing` | no | Sharing with partner organizations (never for advertising). |

Required consents cannot be revoked without also filing an erasure
request — the frontend disables the toggle and the backend still
records the attempt in the audit log if the API is hit directly.

## 4. Erasure scopes

A single erasure request carries a `scope` indicating how far the
deletion should reach. Supported values:

| Scope | Effect |
|---|---|
| `self` | Full account anonymization (email, password, OIDC subject, display name redacted). Private unshared studies deleted; public studies transferred to the anonymous `public` subject; Health Records where the user is `self_user_subject_id` have their PII nullified. Default for user-facing requests. |
| `studies` | Only studies owned by the user are deleted/anonymized; the account remains usable. Useful for professionals cleaning up historical data. |
| `annotations` | Grants issued by the user are revoked, annotation author references nulled. Account remains usable. |
| `consents_only` | Revoke all active consents. Data is retained but consent flags flip to "not granted". |

## 5. What is retained after erasure

GDPR Art. 17(3) enumerates cases where the controller *may* retain data
despite a deletion request — legal obligation, public interest archiving,
exercise of legal claims, etc. bitvision phoenix retains the following:

- **Audit log** (`audit_log` table). Rows about the user are kept; the
  `actor_subject_id` foreign key still points at the (now anonymised)
  user row. Rationale: Art. 17(3)(b) + (e) — compliance with legal
  obligations around traceability of access to health data, and defense
  of legal claims. The display_name of the anonymised user is literally
  "Erased User" so the trail does not surface PII.
- **Consent history**. Consents are *revoked* (`revoked_at = now()`),
  not deleted, per Art. 7(1) — we must be able to prove what the user
  consented to at any point in time.
- **Public studies**. Studies the user previously published (`is_public
  = true`) remain available. The user released them under a
  public-domain-equivalent grant and cannot retroactively withdraw
  downstream users; ownership is re-pointed to the anonymous `public`
  subject so no personal data remains attached.
- **Erasure request row itself** — marked `completed` with a timestamp
  so we can prove the request was honored.
- **Versioning skeleton**. The DAG of `commits` / `manifest_entries` /
  `entity_objects` referenced by patients tied to the erased user is
  preserved (deleting them would break content-addressed hashes used
  by other patients via dedup). The PII payload itself is *tombstoned*
  — see §5.1.

### 5.1 Tombstoning of versioned content (F12.7)

Patient fascicoli hold their clinical content inside the F12 versioning
tables (`entity_objects`, `manifest_entries`, `commits`). Hard-deleting
those rows would invalidate hash chains used by other patients (rows
are content-addressed, so identical clinical text dedups across
fascicoli), so we *tombstone* instead: payload is zeroed, `is_tombstoned
= true`, `tombstoned_at = now()`, `tombstoned_reason = "gdpr.erasure_request:<id>"`.
Readers (`read_object`, `read_at_commit`) surface `{"_tombstoned": true}`
as a marker so the UI can render "[contenuto cancellato per richiesta
erasure]" without seeing the original text.

`services/erasure.py` step 4b implements the scoping:

```sql
WITH erased_objs AS (
  SELECT DISTINCT me.object_hash
  FROM manifest_entries me JOIN commits c USING (commit_hash)
  WHERE c.patient_id = ANY(:erased_patient_ids)
    AND me.entity_kind != '_tree_'
),
refs_elsewhere AS (
  SELECT DISTINCT me.object_hash
  FROM manifest_entries me JOIN commits c USING (commit_hash)
  WHERE me.object_hash IN (SELECT object_hash FROM erased_objs)
    AND NOT (c.patient_id = ANY(:erased_patient_ids))
)
UPDATE entity_objects
SET payload = '{}'::jsonb, is_tombstoned = true,
    tombstoned_at = :now,
    tombstoned_reason = 'gdpr.erasure_request:' || :req_id,
    delta_bytes = NULL, delta_parent_hash = NULL,
    storage_kind = 'full'
WHERE object_hash IN (
  SELECT object_hash FROM erased_objs
  EXCEPT SELECT object_hash FROM refs_elsewhere
);
```

Two key invariants enforced by the `EXCEPT`:

1. **Cross-patient safety**: an `object_hash` shared with the manifest
   of any non-erased patient is left untouched. This protects the
   fascicolo of every other user from being silently scrubbed when
   their (independent) clinical text happens to canonicalise to the
   same SHA-256.
2. **Pack-chain repair**: tombstoning resets `storage_kind = 'full'`
   and clears `delta_bytes` / `delta_parent_hash`, so a tombstoned
   row is no longer a viable parent in any future delta chain. Test
   `test_versioning_security.py::TestErasureScrubsVersioningContent`
   covers both invariants.

`scope='self'` is the only erasure scope that triggers tombstoning;
`scope='studies'` and `scope='annotations'` operate on DICOM /
ownership data and do not touch the versioning blobs.

Pre-F12.7 behaviour: erasure anonymised the `Patient` row's
demographics but the clinical_note bodies remained readable in
`entity_objects.payload` for any historical commit. The fix closes
that residual leak.

## 6. Two-step erasure workflow

1. User files the request via `POST /api/gdpr/erasure-request` (or an
   admin does on their behalf).
2. The request is persisted with `status = 'pending'`.
3. When `scope = 'self'` and no active legal hold is attached, the
   request is auto-approved and `services.erasure.execute_erasure` runs
   in the same transaction, flipping status to `completed`.
4. For non-self scopes or when a legal hold is detected, the request
   stays `pending` and is surfaced to admins for manual review. Admins
   flip status to `approved` (triggering execution) or `rejected` (with
   a note in `data_erasure_requests.notes`).

## 7. Data portability export

`GET /api/gdpr/export` returns a ZIP archive containing:

- `manifest.json` — every record the platform holds about the caller:
  `user`, `consents`, `erasure_requests`, `studies`, `reports`,
  `annotations`, `patients`, `patient_documents`, `audit_log`.
- `README.txt` — human-readable cover note.

DICOM pixel data is **not** embedded in the bundle because it would
balloon the archive into the gigabyte range; use the per-study
download endpoint if raw pixels are needed.

## 8. Security review checklist

Before every release, verify:

- [ ] No new code path writes PII to log files (structured logging only
      emits subject ids, not emails).
- [ ] Any new table holding PII is enumerated in `services.erasure`.
- [ ] Any new entity_kind written through the F12 versioning service
      is automatically covered by the tombstone scope (the SQL above
      is entity-kind-agnostic) — no extra wiring required.
- [ ] Any new consent kind is added to `CONSENT_KINDS` (backend) and
      `CONSENT_LABELS` (frontend) at the same time.
- [ ] `/api/gdpr/export` includes any newly-added PII table.
- [ ] CHANGELOG notes the GDPR impact.
- [ ] `tests/test_versioning_security.py::TestErasureScrubsVersioningContent`
      passes — exhaustive proof that erasure strips clinical content
      from `entity_objects` while preserving cross-patient dedup.

## 9. Device-side storage in the installed app

The frontend is installable as a Progressive Web App
(`frontend/src/app/manifest.ts`), which means it registers a service
worker (`frontend/public/sw.js`), and a service-worker cache is a store
of whatever passes through it. It is enumerated here because a store of
personal data has to be named even when the answer is "it holds none".

| What | Where | Contents | Retention bound | Erasure path |
|---|---|---|---|---|
| Application-shell cache | The viewer's browser, origin-scoped | The content-hashed JS/CSS bundle, the installed-app icons, and the static `/offline` page. **No personal data by construction.** | Until sign-out, or until the browser evicts it | Signing out (`clearServiceWorkerCaches`, called from `lib/auth-context`), clearing site data, or uninstalling the app |

The exclusions are enforced in the worker rather than assumed:
`/api/**`, `/shared/**`, `/viewer/**` and `/_next/image` are passed
straight to the network and never written to a cache, and a navigation
response — which for `/patients/{id}` is the record itself, server
rendered — is fetched fresh every time and never stored. The
`/offline` page exists so a failed navigation is legible; it is the
only mutable thing in the cache and it is static.

This is why the installed app has no offline mode for clinical
content. Making a record readable without a connection means writing it
to the device, which is a data-protection decision with its own
retention bound and its own erasure path, not a caching tweak. It has
deliberately not been taken.

**Not covered here**: `localStorage` and `sessionStorage` usage by
individual components (viewer layout preferences, the theme choice).
Those predate this section and hold interface state rather than
personal data; a sweep confirming that is an open item below.

## 10. Open items

- Legal-hold table (`legal_holds`) is a placeholder; the predicate in
  `services.erasure._user_has_legal_hold` returns `False` today. A
  follow-up migration adds the table and wires it into the erasure
  flow.
- Admin-facing review UI for non-self erasure requests is not yet
  implemented (API is sufficient for DPO tooling).
- Postgres-level RLS policies enforcing consent-based access (e.g. "row
  visible only to callers who hold research_use consent") will land
  once the application-layer predicates in `services/permissions.py`
  are ported to RLS.
- A sweep of every `localStorage` / `sessionStorage` key the frontend
  writes, confirming each holds interface state rather than personal
  data, and listing them in section 9 alongside the service-worker
  cache.
