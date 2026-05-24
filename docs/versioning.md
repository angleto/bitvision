# Git-like versioning of the patient Fascicolo (F12)

Every change to a patient's Fascicolo is a traceable, reversible, and
collaborative act. The system replicates git internals in PostgreSQL:
content-addressed blobs, commit DAG, mutable branches, pull requests.
The user-facing metaphor is "the consultation": the doctor opens a
consultation, writes, closes and proposes; the owner approves or
rejects. The git logic stays invisible by default, exposed only to
`versioning:advanced` users.

This applies to all cases: private, organization, OpenData. Same schema,
same API; the distinction is given by the patient owner.

## Index

1. [Model: 8 tables, 1 service](#modello)
2. [Branch convention](#branch-convention)
3. [The consultation as the single metaphor](#consultation-as-fork)
4. [Owner direct write vs consult](#flusso-owner)
5. [AI conservative-by-default](#ai-conservative)
6. [GDPR via tombstoning](#gdpr)
7. [Storage efficiency: pack-on-GC](#storage)
8. [API endpoints](#api)
9. [Frontmatter advanced mode](#advanced-mode)
10. [Security invariants (F12.7)](#security-invariants)
11. [Cold-tier on S3 (F12.8)](#cold-tier-s3)

## <a id="modello"></a>1. Model: 8 tables, 1 service

| Table | Git equivalent | Role |
|---|---|---|
| `entity_objects` | blob | Immutable content-addressed payload (sha256 of canonical JSON RFC 8785). Schema_version for progressive migrations. GDPR tombstoning. |
| `commits` | commit | DAG anchor: parent_hashes BYTEA[], tree_hash, author + AI provenance, message, db_txid forensic. |
| `manifest_entries` | expanded tree | One row per `(commit_hash, entity_kind, entity_id) → object_hash`. O(1) read for "state at a commit". HASH-partitioned on 16 buckets. |
| `refs` | branch ref | `(patient_id, ref_name) → commit_hash` mutable. Visibility private/shared/public. |
| `ref_log` | reflog | Append-only of every ref move. op_kind ∈ init/commit/merge/reset/revert/rebase/delete. |
| `proposals` | PR | Source_ref → target_ref, status, reviewer, merge_commit. 1:1 with consultations. |
| `merge_conflicts` | (extension) | Per-proposal cache of conflict detection. User-driven resolution via UI. |
| `binary_blobs` | (extension) | S3 indirection for large files (NIfTI segmentation, PDF) with content-hash + refcount for GC. |

The "repository" is the **patient**. All commits/refs have direct
`patient_id`; the other tables join via commit. Patient-scoped RLS
enforced in the DB (not only at the application layer).

Single service: `bvphoenix.services.versioning` exposes `commit_change`,
`read_at_commit`, `diff_commits`, `read_object` + consultation helpers
(`open_consultation_branch`, `resolve_branch_for_write`,
`submit_consultation_proposal`, `fast_forward_merge`).

## <a id="branch-convention"></a>2. Branch convention

Only two names are valid for a new branch:

| Pattern | When created | Owner | Lifetime |
|---|---|---|---|
| `main` | always, lazy-seeded at the first write or consultation open | `patients.managed_by_subject_id` (or `PLATFORM_OWNER` for OpenData) | persistent |
| `consultation/<consultation_id>` | automatically when a consultation is opened | the consultation author | as long as the consultation is draft/submitted, archived after merge/reject |

Never shown to the user. In the viewer / Fascicolo we speak of
"ongoing consultation", "approved changes", "history". The commit hash
appears only in advanced mode.

## <a id="consultation-as-fork"></a>3. The consultation as the single metaphor

The existing `consultations` model becomes the user-facing wrapper of
the fork+PR. The state machine is already a perfect fit:

| Git concept | User concept | State in `consultations` |
|---|---|---|
| `git checkout -b consultation/<id>` | "I open a consultation" | INSERT with `status='draft'` or `'submitted'`, branch created |
| commit on the branch | save of a note / tag during the consultation | row in commits, ref bumped |
| `git push` + open PR | "Close consultation and propose changes" | `status='draft' → 'submitted'`, proposals row created |
| review/approve & merge | "Approve changes" | `status='submitted' → 'reviewed'`, merge commit on main |
| close PR without merge | "Reject changes" | `status='rejected'`, branch locked |

The technical `proposals` record stays backstage: the user only sees
the consultation. Signing the consultation calls `fast_forward_merge`
if the base is unchanged; otherwise the proposal stays `open` and waits
for the three-way merge engine (F12.3).

## <a id="flusso-owner"></a>4. Owner direct write vs consult

When the doctor owner modifies their own Fascicolo, **no consultation
is opened**. Their writes go directly to `main`. Same audit trail
(ref_log, commit, audit_log), zero friction. Preserves the Drive
experience: "save and that's it", the history is there but not in your
face.

`resolve_branch_for_write` decides:

```
if consultation_id:
    -> 'consultation/<id>' (verifies the consultation is draft/submitted
       and belongs to the actor)
elif is_owner:
    -> 'main' (direct commit)
else:
    -> 403 "consultation required" (you must open a consultation first)
```

## <a id="ai-conservative"></a>5. AI conservative-by-default

Every invocation of an agent token that mutates the Fascicolo opens
(or reuses) a consultation `kind='ai'`. Three invariants:

1. **Dedicated branch**: `consultation/<id>`. The agent commits only
   there, never on main. Even if `agent_tokens.permissions` included
   `note:write`, the versioning service force-resolves to the branch.
2. **Provenance in commits**: `author_kind='agent'`, `model_id`,
   `provider`, `agent_token_id` saved on `commits`. Reuses existing
   fields on `consultations` and `clinical_notes`.
3. **Mandatory submit + review**: at the end of the AI task the
   consultation moves to `submitted`. The owner gets a notification.
   No auto-merge. Ever.

Rate limit per agent_token: configurable max N consultations/hour,
enforced by the versioning service (rejection of `commit_change` with
429). Tokens that violate the limit are auto-revoked.

## <a id="gdpr"></a>6. GDPR via tombstoning

For erasure (Art. 17): do NOT delete `entity_objects` rows (this would
break the hash chain + audit). They are **tombstoned**:

```sql
UPDATE entity_objects
SET payload = '{}'::jsonb,
    is_tombstoned = true,
    tombstoned_at = now(),
    tombstoned_reason = 'gdpr.erasure_request:<id>'
WHERE object_hash IN (objects of the patient to erase);
```

The object_hash remains valid (it's the byte string). Anyone
reconstructing a past state sees `{}` with a flag and the label
"[erased per request <id>]". `binary_blobs` referenced: tombstone +
decrement refcount; when refcount reaches 0, a worker deletes the
physical file from S3.

`services/erasure.py` performs the tombstoning as step "4b" of the
`scope='self'` flow, scoped via `EXCEPT` so it does not touch any
object_hash shared with commits of other patients (see
[§10.3](#security-invariants)). One audit_log entry per erasure
request (`gdpr.erasure_executed`).

## <a id="storage"></a>7. Storage efficiency: pack-on-GC like git

Git has loose objects and pack files. `git gc` moves loose objects into
packs and optimizes existing chains. We replicate the same logic to
avoid having a one-character change in a clinical_note.body of 50 KiB
write a new 50 KiB blob.

**Write path (synchronous, zero latency)**: `entity_objects` is ALWAYS
written in full form (`storage_kind='full'`). TOAST + pglz save ~50%
on repetitive text.

**Pack worker (asynchronous, F12.6)**: analyzes chains of
entity_objects for the same `(entity_kind, entity_id)`. After
N >= 10 versions:
- computes the bsdiff delta between payload(v_n-1) and payload(v_n)
- if `len(delta) < 0.5 * len(full)`, replaces v_n with
  `(storage_kind='delta', delta_parent_hash, delta_bytes zstd-compressed)`
- full snapshot every 10 versions (bounded chains)

The schema has been ready since F12.0 (columns `storage_kind`,
`delta_parent_hash`, `delta_bytes` with CHECK invariants). The read
path is transparent: `read_object` checks storage_kind and
reconstructs from the chain.

Storage estimate: average delta:full ratio of 0.5 on text → 60%
saving. On 100k patients, 200 commits/year, 5 entities/commit:
~3 MiB/patient/year.

Large binaries (NIfTI, PDF) do NOT enter the delta scheme: they live
in `binary_blobs` with S3 content-hash, every unique blob is one S3
object.

## <a id="api"></a>8. API endpoints (F12.0+F12.1 landed)

### Pilot endpoints (clinical_notes)

```
POST   /api/patients/{id}/notes?consultation=<id>
PATCH  /api/patients/{id}/notes/{nid}?consultation=<id>
DELETE /api/patients/{id}/notes/{nid}?consultation=<id>
```

`?consultation=` optional. If absent and the actor is owner → main; if
present → consultation branch; non-owner without consultation → 403.
Atomic dual-write: clinical_notes table + entity_objects + commit +
manifest_entries + ref_log + audit_log in the same transaction.

### Consultation lifecycle

```
POST   /api/consultations                  -> opens branch, optionally proposal
PATCH  /api/consultations/{id}             -> edit draft (no merge)
POST   /api/consultations/{id}/sign        -> fast_forward_merge if possible
POST   /api/consultations/{id}/reject      -> withdraw proposal, lock branch
GET    /api/consultations/{id}             -> details (including branch info)
GET    /api/patients/{id}/consultations    -> list
```

Under the hood:
- POST consultation → `open_consultation_branch` + (if status='submitted')
  `submit_consultation_proposal`
- sign → `fast_forward_merge` or (if diverged) status='signed' without
  merge, awaiting F12.3
- reject → UPDATE proposals.status='withdrawn' + UPDATE
  refs.is_locked=true

### New endpoints (future, F12.5)

```
GET /api/patients/{id}/history             -> commits timeline
GET /api/patients/{id}/at/<commit>/...     -> state at a specific commit
GET /api/patients/{id}/diff?from=<a>&to=<b> -> diff between commits
```

## <a id="advanced-mode"></a>9. Advanced mode

User profile toggle "Show versioning details". When active:
- commit hashes next to history entries
- JSON diff in the review panel
- share-link with `?at=<hash>` for time pinning
- API endpoints documented in OpenAPI

Default OFF. Permission `versioning:advanced` granted by an
organization admin enables the view. The flag lives in user
preferences (localStorage + optional server-side persistence).

## Implementation references

- `backend/src/bvphoenix/services/canonical.py`: RFC 8785 + tests
- `backend/src/bvphoenix/db/models/versioning.py`: 8 models
- `backend/alembic/versions/0035_versioning_schema.py`: schema migration
- `backend/alembic/versions/0036_platform_owner_subject.py`: PLATFORM_OWNER
- `backend/src/bvphoenix/services/versioning.py`: core service
- `backend/src/bvphoenix/api/clinical_notes.py`: refactored pilot endpoint
- `backend/src/bvphoenix/api/consultations.py`: consultation lifecycle hooks
- `backend/tests/test_canonical.py` (26 tests) + `test_versioning.py` (15 tests)
- `plans/zazzy-honking-nygaard.md`: complete implementation plan

## Roadmap

| Phase | Status | What |
|---|---|---|
| F12.0 | landed | canonicalizer, schema, commit_change, pilot clinical_notes |
| F12.1 | landed | consultation-as-fork, fast-forward merge, reject |
| F12.2 | landed | PLATFORM_OWNER + OpenData visibility |
| F12.2d | landed | auth tightening (`require_auth_globally` setting + explicit `public_user` dep for a2a/shared) |
| F12.3 | landed (backend) | three-way merge engine + proposals API + conflict resolution endpoints |
| F12.3 UI | pending | conflict resolver UI (clinical language vs advanced JSON diff) |
| F12.4 | landed | de-id regex baseline + OpenData publish endpoint (clone-and-scrub) + optional LLM scrub (?use_llm_scrub=true) for proper names / narrative context |
| F12.5 | landed | time-travel API + history viewer + diff viewer frontend |
| F12.6 | landed | pack worker (zlib dictionary delta encoding, snapshot every 10) |
| F12.7 | landed | security hardening: read-after-pack, cross-proposal ref-lock, GDPR blob tombstoning, extended Italian phone regex, severe-test suite (see §10) |
| F12.8 | landed | cold-tier on S3: ``storage_kind='s3'`` + tier-down worker for old large entity_objects, transparent reads through ``read_object`` (see §11) |

## <a id="security-invariants"></a>10. Security invariants (F12.7)

A dedicated severe-testing pass surfaced four latent bugs that the
happy path was hiding. The fixes landed together with a suite of 80
tests that pin the invariants as a contract. The module must not
regress on any of these without an explicit ADR.

### 10.1 Read after pack worker

`read_at_commit` now transparently resolves `entity_objects` rows
with `storage_kind='delta'` by delegating to `read_object`, which
walks the zlib delta chain back to the closest full snapshot.
Pre-F12.7, any historical commit whose entity had been compressed by
the pack worker (F12.6) raised `NotImplementedError` on read.

Test pinning the invariant:
`test_versioning_security.py::TestTombstoneAndDeltaIntegrity::test_read_at_commit_resolves_packed_delta_objects`.

### 10.2 Cross-proposal serialisation of fast-forward merge

`fast_forward_merge` now also locks the target `refs` row
(`SELECT ... FOR UPDATE` on `refs`, on top of the existing lock on
`proposals`). Without the refs lock, two distinct proposals
targeting the same head both read `target_head = X`, both concluded
"no divergence", and the second one's `UPDATE refs` silently
overwrote the first (lost-update of a whole merge).

Test:
`test_versioning_extras.py::TestConcurrentMergesToMain::test_truly_parallel_ff_merges_one_wins_one_loses`
fires two real `fast_forward_merge` calls on independent engines via
`asyncio.gather` and requires exactly one OK and one diverged.

### 10.3 GDPR tombstoning of blobs (entity_objects)

`execute_erasure` with `scope='self'` now tombstones the payloads of
`entity_objects` referenced by commits of patients **self-owned** by
the erased user. The logic uses a SQL set-difference (`EXCEPT`) so
it leaves untouched any object_hash shared with commits of other
patients: `entity_objects` is content-addressed (sha256 of canonical
JSON), so two identical short notes by different users dedup to the
same blob.

Pipeline:

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

Note that the tombstone resets `storage_kind = 'full'` and clears
the delta fields: a tombstoned blob can no longer act as a parent
for new delta chains, and readers see the `{"_tombstoned": true}`
marker instead of trying to reconstruct a payload from a broken
chain.

Test:
`test_versioning_security.py::TestErasureScrubsVersioningContent`
has two cases: a positive one (exclusive blobs are tombstoned) and
a negative one (blobs shared with other patients remain readable).

### 10.4 Cross-patient leak via /at, /history, /diff, /ref-log

Every history-side endpoint (`api/history.py`) validates *defense in
depth* that the cited commits / hashes belong to the URL patient
before answering. The 404 responses are **identical** in shape
whether the commit does not exist or belongs to another patient:
no timing oracle.

Test: `test_versioning_security.py::TestCrossPatientLeak` (8 cases)
covers all four endpoints with adversarial scenarios, including the
404 response-shape parity.

### 10.5 Extended de-id phone regex

The `_PHONE_RE` pattern in `services/deid_text.py` now allows an
optional separator (` `, `-`, `.`) between every digit after the
prefix, so it matches `333 123 4567`, `333-123-4567`,
`333.123.4567`, and the `+39` variants
(`+39 333 1234567`, `+39 333.123.4567`, etc.). Pre-fix, a second
separator (e.g. two spaces) made the match fail and the number
leaked through publish.

Test: `test_deid_text_italian.py::TestPhone` uses `REDACTED` /
`MISSED` matrices: the `MISSED` list is the contract for known
limitations (non-IT international formats). A future fix that covers
them must flip the entry to `REDACTED`, otherwise the test fails.

### 10.6 Open gap: RLS owner-bypass

`test_versioning_security.py::TestRlsStructuralEnforcement` is
marked `xfail`. It documents that the RLS policies in
`0009_rls_policies.py` and `0035_versioning_schema.py` are
decorative when the app role coincides with the table owner
(Postgres bypasses RLS for owners unless `FORCE ROW LEVEL SECURITY`
is enabled). See `docs/security-rls.md` §"Production hardening
required" for the two paths to close the gap (FORCE RLS via a new
migration with `set_config 'service'` at the top of every DML
migration, or a deploy with an app role distinct from the table
owner).

## <a id="cold-tier-s3"></a>11. Cold-tier on S3 (F12.8)

Pre-F12.8 every `entity_objects` payload lived in Postgres regardless
of age. With ten years of history at scale (∼100k patients × 1k
commits × multiple textual entities) the table grows into the TBs
even with the F12.6 delta encoding. F12.8 introduces a third storage
tier so cold rows leave the hot path of Postgres and live on S3 /
MinIO under the per-deployment versioning bucket.

### 11.1 Storage state machine

`entity_objects.storage_kind` now takes one of three values, with a
disjoint column-set CHECK invariant per tier:

| storage_kind | Hot columns | Read path |
|---|---|---|
| `'full'` (default on write) | `payload` (canonical JSONB) | inline lookup |
| `'delta'` (F12.6 pack worker) | `delta_parent_hash`, `delta_bytes` | walk back to a full snapshot, decompress |
| `'s3'` (F12.8 tier-down worker) | `s3_bucket`, `s3_key`, optional `s3_etag` | download canonical bytes from S3, parse |

The transitions are one-way: writes always start at `full`, the pack
worker can demote `full → delta` (subject to its own size threshold),
the tier-down worker can demote `full → s3` for older rows. Going
back is intentionally not exposed; tombstoning resets the row to
`full + payload='{}'` and clears every indirection (see §10.3 plus
the F12.8 erasure update that also nulls `s3_*`).

### 11.2 Tier-down worker

`services.versioning.tier_down_entity_objects(...)` walks
`entity_objects` rows that satisfy the intersection of:

* `storage_kind = 'full'`,
* `is_tombstoned = false` (GDPR-erased rows stay where they are),
* `payload_size >= min_payload_bytes` (default 16 KiB; below that
  the metadata cost beats the storage saving),
* `created_at < now() - INTERVAL '<age_days> days'` (default 365 days;
  recent history must remain hot to keep the live read path off S3).

For each candidate it canonicalises the payload (RFC 8785), uploads
the bytes to `s3://<bucket>/entity_objects/<hash[:2]>/<hash>` (sharded
by the first two hex chars of the object hash so prefix scans don't
balloon), then flips the row in place: `storage_kind='s3'`,
`payload=NULL`, `s3_bucket / s3_key` populated. The UPDATE only
matches rows still at `storage_kind='full'`, so a concurrent run
from another worker is a no-op on the second pass.

The function processes up to `batch_limit` rows per call (default
200) so a long backlog can be drained by repeated invocations
without holding open transactions or saturating boto3 connections.
Caller MUST be inside a DB transaction; the S3 upload happens before
the row update so a partial run leaves the bytes available on S3
and idempotent on the same hash.

No reachability check is performed in this iteration: a row's
eligibility is its absolute age, not whether the recent DAG of any
ref still references it. The trade-off is that an old commit's
occasional read may hit S3, which is the intended cold-tier
behaviour. A future iteration can layer a reachability filter on top
to guarantee no S3 round trip for any commit reachable from a
current ref's HEAD within K hops.

### 11.3 Read path

`read_object` and `read_at_commit` resolve the s3 tier transparently.
The boto3 GET is wrapped in `asyncio.to_thread` so the API event loop
keeps moving while the S3 round trip runs on a worker thread. The
canonical-JSON round trip through `_bytes_to_payload` is byte-stable,
so dict equality with the original payload holds.

For chains, the s3 tier acts as the bottom of the walk: a delta whose
chain ends at an s3-tier full row downloads the bytes from S3 once
and then replays the in-memory deltas back down the chain. There is
no schema rule that forbids tiering down a delta-anchor full row
ahead of the deltas that depend on it — the worker simply moves the
canonical bytes; the pack chain still resolves through the same
`read_object` path.

### 11.4 GDPR notes

Tombstoning is unchanged in semantics: the erasure UPDATE sets
`payload='{}'`, `is_tombstoned=true`, clears every indirection
column (`delta_*` AND `s3_*`), and forces `storage_kind='full'`.
After erasure the read path returns `{"_tombstoned": true}` without
touching S3.

The S3 object that backed an erased row is **not** automatically
deleted by the erasure UPDATE: leftover canonical bytes for an
erased payload remain in the cold-tier bucket. A separate sweep is
required to garbage-collect them; the simplest implementation is a
periodic `LIST entity_objects/` against the tier-down bucket
crossed against `entity_objects` (`s3_key IS NULL OR is_tombstoned`)
to surface keys that no longer have a row pointing at them. Tracked
as an open follow-up; the audit-log entry already records the
intent ("`gdpr.erasure_request:<id>`"), so the deletion can be
reconciled offline.

### 11.5 Configuration

```env
BVP_S3_BUCKET_VERSIONING=bvphoenix-versioning   # default
```

The bucket is auto-created (`storage.S3Storage.ensure_bucket`) on the
first tier-down call. Server-side encryption follows the
deployment-wide `BVP_S3_ENCRYPTION` setting (`AES256` by default,
`aws:kms` with a KMS key, or `none` for local MinIO without KES).

### 11.6 Tests

`test_versioning_s3_tier.py` (5 cases) covers:
* round-trip: tier-down then `read_object` returns the same payload;
* recent rows are skipped (age filter);
* below-threshold rows are skipped (size filter);
* second worker call on the same backlog is a no-op (idempotent);
* tombstoned rows are skipped and reads short-circuit to the marker.

The pack worker (F12.6) and tier-down worker (F12.8) are independent
and may both touch the same logical entity at different times; their
selection criteria do not overlap (pack pulls only from a
manifest-bound chain, tier-down pulls only large old rows), so order
is irrelevant.

### 11.7 Open follow-ups

**Orphan S3 objects after GDPR erasure.** The erasure UPDATE
(`services/erasure.py`) clears `s3_bucket` / `s3_key` on the row but
does NOT delete the object on S3. A reconciliation sweep is needed:
`LIST` the tier-down bucket prefix `entity_objects/`, anti-join
against the `entity_objects` table (`s3_key IS NULL OR
is_tombstoned`), `DELETE` the orphan keys. Suggested cadence: once a
day off-peak, with a metric on the orphan-rate. Until this lands,
the deployment is GDPR-compliant only at the DB level; the S3
bucket retains canonical bytes that point at no live row. Audit
correlation is preserved via `tombstoned_reason =
'gdpr.erasure_request:<id>'` so the sweep can defer to the same
request id when emitting its delete log.

**Reachability filter on the tier-down candidate set.** The current
worker selects on absolute age (`created_at < now() - <age_days>`).
A row that is still referenced by the recent DAG (e.g. a clinical_note
that has not changed in 18 months but is the head of `main` for that
note's entity_id) will get tiered down and incur an S3 round trip
on the live read path. A future iteration should layer a
reachability check: "skip if any commit reachable within K hops from
any current ref's HEAD references this object_hash". This requires a
walk over the recent-DAG manifest entries; cost is bounded but not
trivial. Defer until live read latency from the cold tier is shown
to be a real problem in production.

**S3 object tagging for compliance audit.** Each tier-down upload
could set object tags (`patient_id` of the originally-owning commit,
`erased=false/true`, `created_at`) so an external compliance scan
can answer "show me all canonical bytes belonging to patient X" via
S3 alone, without DB access. Deferred: needs a tag-vs-RLS leak
review first (object tags are not encrypted, so embedding
`patient_id` is a plaintext identifier in the bucket inventory).

## History & time-travel API (F12.5 backend)

Four read-only endpoints for the history. Permission: `READ_METADATA`
on the patient, same as the rest of the patient API.

```
GET /api/patients/{id}/history?ref=main&limit=100
  -> { patient_id, ref_name, head_commit, commits: [...] }
  Linear walk of parent[0] from head; for merge commits, follows the
  target side (mainline). Full DAG view in advanced (future).

GET /api/patients/{id}/at/{commit_hash}?entity_kind=clinical_note
  -> { entity_kind: { entity_id: payload, ... }, ... }
  Reconstructs the state at a specific commit via manifest.
  Tombstoned -> {"_tombstoned": true}.

GET /api/patients/{id}/diff?from=<a>&to=<b>
  -> [ { entity_kind, entity_id, change ∈ added|removed|modified,
         hash_a, hash_b }, ... ]
  Equal entries omitted.

GET /api/patients/{id}/ref-log?ref=main&limit=100
  -> [ { ref_name, from_commit, to_commit, op_kind, actor, reason,
         created_at }, ... ]
  Sorted newest first. op_kind: init/commit/merge/reset/revert/rebase/delete.
```

All validate that the commits belong to the target patient (defense
in depth + clear 404 vs empty result via RLS).

## Publish to OpenData (F12.4, regex landed)

`POST /api/patients/{id}/publish` clones-and-scrubs a private Fascicolo
into a new public Fascicolo owned by `PLATFORM_OWNER`. The private one
is never mutated. Pipeline:

1. Permission check: only owner or admin.
2. Demographic anonymization: name → pseudonym, tax_id/email/phone/
   address → null, birth_date → year-only (January 1st of the year),
   contacts → []. allergies/notes go through `redact_text`.
3. For each `clinical_note` of the private record (F12.4 v0 scope:
   notes only, other types deferred): `redact_text(body)`, clone with
   `author_subject_id = PLATFORM_OWNER`, one `redaction_events` row
   per redaction applied.
4. Seed `main` of the new patient with commit
   `[opendata] initial publication` containing seed entity + all
   cloned clinical_notes.

`services/deid_text.py` regex baseline covers: Italian tax code
(codice fiscale), email, phone (Italian mobile / landline, +39
prefix), precise dates (DD/MM/YYYY, YYYY-MM-DD), addresses
(`via X N`). Proper names NOT covered by regex (too noisy): the
LLM scrub via MCP is F12.4-llm. For now the demographic strip on
the private side eliminates the dominant leak.

Audit: `redaction_events(target_kind, target_id, field_path,
original_excerpt_hash, redaction_kind, applied_by_subject_id, applied_at)`.
Append-only, RLS visible to all authenticated users (the row contains
only the hash of the original excerpt, not the plaintext).

```
POST /api/patients/{id}/publish
  body: { "pseudonym": "OpenData Test 001" }      -> optional
  201: { public_patient_id, public_main_commit, cloned_clinical_notes, redaction_count }
```

## Three-way merge (F12.3 backend landed)

The merge engine handles divergent histories: an owner who modifies
`main` while a consultation is pending. Four conflict categories:

| Case | Meaning |
|---|---|
| `add_add` | Same entity_id created on both sides starting from a base that did not contain it |
| `edit_edit` | Both sides modify the same entity with different hashes |
| `edit_delete` | Source modifies, target deletes |
| `delete_edit` | Source deletes, target modifies |

`detect_conflicts(base, source, target)` returns the list of
`DetectedConflict` for the review UI; `three_way_merge(...)` accepts
a list of `MergeResolution` (`take_source` / `take_target` / `manual`
with pre-computed object_hash) and produces a merge commit with
`parent_hashes=[target, source]`. If resolutions are missing for any
conflict, raises `ConflictsUnresolved` with the list to prompt for.

`submit_consultation_proposal` now pre-computes conflicts at the time
the proposal is opened and populates `merge_conflicts`, so the review
does not recompute the diff on every page load.

### API endpoints

```
GET    /api/proposals/{id}                                  -> details + conflicts
GET    /api/patients/{id}/proposals?status=open             -> list
POST   /api/proposals/{id}/conflicts/{cid}/resolve          -> records take_source/take_target/manual
POST   /api/proposals/{id}/merge                            -> fast-forward or three_way
POST   /api/proposals/{id}/withdraw                         -> drop without merge
```

Permissions:
- read: anyone with `read:metadata` on the patient
- resolve / merge: only patient owner or platform admin
- withdraw: the proposer (self-withdraw) or the owner (rejection)
