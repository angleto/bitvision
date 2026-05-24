# Encryption at rest: application-side envelope encryption (design)

**Status**: design only, not implemented. This document captures the
target architecture for envelope encryption of patient data so a future
implementation has a fixed reference. No code in this repository
currently implements per-patient DEKs or wraps DICOM blobs at the
application layer beyond what is described in §2.

The motivating requirement is twofold: (a) keep the cleartext of
patient health records out of the trust boundary of the storage layer
(Postgres operator, Object Storage operator, backup tape custodian),
and (b) make GDPR Art. 17 ("right to be forgotten") satisfiable with
high assurance even when the underlying data lives in immutable
backups, replicas, or versioned object storage.

The mechanism that addresses both, and that we adopt here, is
**envelope encryption with per-patient Data Encryption Keys (DEKs)**
plus **crypto-shredding** as the erasure primitive.

## 1. Goals and non-goals

### Goals

1. Plaintext of patient-identifying or clinically-sensitive data is
   never present in Postgres, Object Storage, or backups.
2. Erasure of a patient is achievable by destroying a single key, with
   no requirement to overwrite ciphertext or rewrite backups.
3. Key custody is separated from the application: a compromise of the
   backend host does not, by itself, expose all patient plaintexts at
   once.
4. Key rotation (KEK level) does not require re-encrypting any
   patient data, only re-wrapping DEKs.
5. The design is incremental: layer 1 (S3 SSE) and layer 2 (volume
   encryption) remain in place and continue to provide defense in
   depth.

### Non-goals

1. Encrypting *every* column. The aim is to wrap data classified as
   "patient-identifying" or "clinical content", not, for instance,
   model registry rows or audit logs (which need queryability).
2. Searchable encryption beyond exact-match blind indexes.
   Full-text search over encrypted clinical notes is **out of scope**;
   if needed, it ships through a separate plaintext search index
   inside the application's trust boundary, governed by the same
   crypto-shred lifecycle.
3. Defending against a compromised backend process at runtime. Once a
   DEK is unwrapped in backend memory to serve a request, the
   plaintext is, by construction, accessible to that process.
   Memory-safety, syscall sandboxing, and short DEK cache TTLs
   mitigate but do not eliminate this.
4. Hardware-rooted attestation (TPM, SEV-SNP, TDX). Out of scope for
   the initial design; the KMS/Vault deployment is the trust anchor.

## 2. What is already in place

We build on, and do not replace, the following existing primitives.

| Layer | Today | File / setting |
|---|---|---|
| Volume encryption | Provided by Scaleway underneath Managed DB and Object Storage. No operator action. | n/a |
| S3 server-side encryption | SSE-S3 (default `AES256`) on every PUT, configurable to SSE-KMS. | `bvphoenix.storage.default_put_extra_args`, `BVP_S3_ENCRYPTION` |
| AES-GCM master key in env | 32-byte key in `BVP_BYOK_MASTER_KEY`, currently used only for `user_api_keys`. | `bvphoenix.services.byok` |
| pgcrypto extension | Enabled in production Managed DB. Available but unused for column crypto today. | `DEPLOYMENT-GUIDE.md` §1.2 |
| DICOM header de-identification | `bvphoenix.services.deidentify` for share-link downloads. | `security-encryption-deidentify-cors.md` |
| Free-text de-identification | Regex pass for OpenData publish, optional LLM scrub. | `bvphoenix.services.deid_text` |

The existing `byok.py` is a useful reference: it already implements
the *symmetric primitive* (AES-GCM with a 12-byte nonce per row,
single env-resident master key). The new design generalises that
pattern, adds a key-wrapping layer on top, and externalises the root
of trust to a KMS.

## 3. Threat model and what envelope encryption adds

Threats considered, and the relative residual risk under each layer:

| Threat | Volume enc. (today) | S3 SSE (today) | Envelope (proposed) |
|---|---|---|---|
| Stolen disk / decommissioned drive | Mitigated | Mitigated | Mitigated |
| Cloud-provider operator reads object storage | Not mitigated | Partially (SSE-KMS with our key) | Mitigated |
| Cloud-provider operator reads database | Not mitigated | n/a | Mitigated for wrapped columns |
| Backup tape exfiltrated | Mitigated only if backup is itself encrypted | Inherited from object | Mitigated |
| Backend process compromised at runtime | Not mitigated | Not mitigated | Not mitigated for in-flight patients |
| Single-patient erasure must persist across all backups | Not satisfiable | Not satisfiable | Satisfiable via crypto-shred |
| Lateral movement from another tenant in shared infra | Not mitigated | Partial | Mitigated |
| Legal demand for a single patient's plaintext after we shredded the DEK | Not satisfiable | Not satisfiable | "Cryptographically destroyed, cannot produce" |

The last row is, in practice, the strongest argument for envelope
encryption in a healthcare context. Volume and SSE encryption answer
"the disk is safe at rest"; envelope encryption answers "we can prove
a specific subject's data is no longer recoverable, even though the
ciphertext bytes still exist somewhere in our backup chain".

## 4. Architecture overview

```
            +---------------------------+
            |  KMS (Vault Transit)      |
            |  Root KEK (HSM-backed     |
            |  if available)            |
            +-------------+-------------+
                          |
                  wraps / unwraps
                          |
                          v
            +---------------------------+
            |  Tenant KEK               |   <-- one per logical tenant
            |  (cached in memory of     |       (single-tenant deploys
            |  backend, never persisted |        collapse this layer)
            |  in plaintext)            |
            +-------------+-------------+
                          |
                  wraps / unwraps
                          |
                          v
            +---------------------------+
            |  Patient DEK              |   <-- one per patient
            |  (32-byte AES-256, stored |
            |  wrapped in DB column)    |
            +-------------+-------------+
                          |
                          | encrypts
                          v
        +-----------------+-----------------+
        |                                   |
        v                                   v
  DICOM blobs in S3                Sensitive DB columns
  (raw bucket)                     (clinical_notes.body,
                                    patients.notes,
                                    patients.allergies, ...)
```

### Key hierarchy in concrete terms

- **Root KEK**: lives in HashiCorp Vault Transit (or Scaleway KMS, or
  AWS KMS, depending on deployment). Never leaves the KMS. All
  wrap/unwrap of tenant KEKs happens by API call. Audited.
- **Tenant KEK**: AES-256, derived per logical tenant. In a single-
  tenant deployment this layer collapses into the root KEK. Cached
  in backend memory with a short TTL (proposed: 5 minutes idle, hard
  cap 1 hour) so that revocation of the tenant from KMS takes effect
  within a bounded window.
- **Patient DEK**: AES-256 + 12-byte nonce per encryption operation.
  Stored in `patient_encryption_keys` as `(patient_id, wrapped_dek,
  kek_version, created_at, shredded_at)`. A patient has exactly one
  *active* DEK; rotation creates a new row and re-encrypts existing
  ciphertexts lazily.

### Why three layers and not two

A flat "KMS wraps DEKs directly" design works, but every patient
operation requires a KMS round-trip to unwrap. With ~100k patients
and burst access this becomes a scaling and cost problem. The tenant
KEK layer reduces KMS calls to once per tenant per cache window and
keeps DEK unwraps local to backend memory.

## 5. What gets encrypted

A non-exhaustive starting list, to be refined during implementation:

### S3 (object storage)

| Bucket / prefix | Today | Proposed |
|---|---|---|
| `<raw-bucket>/` (DICOM originals) | SSE-S3 only | Application-side AES-GCM with patient DEK, per-object nonce |
| `<derivatives-bucket>/thumbnails/` | SSE-S3 only | Application-side encryption, patient DEK |
| `<derivatives-bucket>/exports/` (GDPR ZIP, fascicolo PDF) | SSE-S3 only | Application-side, ephemeral DEK destroyed on download |
| `<derivatives-bucket>/opendata/` | SSE-S3 only | Stays SSE-S3 only. By construction this content is already de-identified for publication. |

### Postgres

| Table.column | Today | Proposed |
|---|---|---|
| `patients.given_name`, `patients.family_name`, `patients.codice_fiscale` | plaintext | wrapped with patient DEK, plus blind index on CF for lookup |
| `patients.notes`, `patients.allergies` | plaintext | wrapped |
| `clinical_notes.body` | plaintext | wrapped |
| `consultations.notes` | plaintext | wrapped |
| `reports.body` | plaintext | wrapped |
| `audit_log.*` | plaintext | **stays plaintext**. Audit must remain queryable; redact PHI before insert instead. |
| `redaction_events.*` | hashes only, by design | unchanged |
| `user_api_keys.key_ciphertext` | AES-GCM with env master key | migrated under the new KMS hierarchy in a separate phase |

### What does NOT get wrapped

- Foreign keys, surrogate IDs, timestamps. Exposing these to the DB
  operator is acceptable; they support indexing and joins. PHI
  inferable from a timestamp pattern (e.g. weekly visits) is a
  separate concern handled at the access-policy layer, not at rest.
- DICOM UIDs. They are not patient identifiers in the GDPR sense
  unless paired with the cleartext linkage table.
- `consents` table content. The proof-of-consent must remain
  inspectable by the DPO without DEK access.

Each of these decisions deserves a paragraph of justification at
implementation time; the table above is the starting point, not the
final word.

## 6. Crypto-shredding for GDPR Art. 17

Erasure flow:

1. `POST /api/gdpr/erasure-request` is approved by an admin.
2. Backend marks `patient_encryption_keys.shredded_at = now()` and
   *overwrites* the `wrapped_dek` column with zero bytes in the same
   transaction.
3. Backend issues a Vault Transit `delete` for any per-patient KMS
   resource (if the tenant KEK layer was bypassed for that patient,
   which is unusual).
4. Audit row is written: `kind = "crypto_shred"`, carrying the
   patient ID and the SHA-256 of the (now-destroyed) DEK so a
   future auditor can verify "yes, this specific key is gone".
5. The ciphertext rows in S3 and Postgres are **left in place**.
   Reads will fail with `KeyShreddedError`, surfaced as HTTP 410
   Gone with a constant body so the response itself does not leak
   "this patient existed".

The point of this design: backups taken before step 2 still contain
the wrapped DEK, but the *unwrapping path* is destroyed. As long as
the tenant KEK has never been exfiltrated outside Vault, no one can
ever unwrap that DEK again. The ciphertext, while bit-for-bit still
present in tape archives, is computationally inaccessible.

This is the GDPR-defensible answer to "but our 7-year backup
retention conflicts with the right to erasure". The answer is: yes
the bytes are still there, no they cannot be read, here is the
audit row proving when the key was destroyed.

### Caveats to call out to legal counsel before relying on this

- The Italian Garante has not, to our knowledge, issued a binding
  opinion that crypto-shredding satisfies Art. 17 in the absence of
  ciphertext deletion. The European Data Protection Board WP29
  guidance on anonymisation (Opinion 05/2014) is *suggestive* but
  predates widespread envelope encryption practice.
- Crypto-shredding is only as strong as the assumption that the KEK
  has not been copied. This implies strict KMS access logs and a
  defensible threat model document.
- It does not satisfy erasure of data that has been *exported* in
  the clear before the shred (e.g. a fascicolo PDF emailed to a
  consultant). That path needs separate controls (`security-gdpr.md`
  §5).

## 7. Migration strategy

Implementation should be **strictly additive, dual-write, then cut
over**. No schema rewrite, no big-bang re-encryption job that locks
production for a weekend.

1. Phase 0: stand up Vault Transit alongside the cluster. Provision
   the root KEK, set up access policies for the backend service
   account only. Verify wrap/unwrap latency and error budget under
   load.
2. Phase 1: introduce `patient_encryption_keys` table, populate one
   row per existing patient with a fresh DEK wrapped under the
   tenant KEK. No application code reads the column yet.
3. Phase 2: add `*_encrypted` columns alongside the existing
   plaintext ones for the targeted tables. Application writes both;
   reads still come from plaintext. Backfill job encrypts existing
   rows in chunks.
4. Phase 3: flip the read path one column at a time, behind a
   feature flag (`BVP_ENVELOPE_READ_<column>`). Roll back is a flag
   flip, not a deploy.
5. Phase 4: remove plaintext columns. Run `VACUUM FULL` once on each
   affected table to reclaim toast space; remember that prior
   plaintext is still on backups (this is acceptable, see §6).
6. Phase 5: same flow for S3 blobs. Wrap on write first, dual-read,
   then a backfill worker re-uploads existing objects with
   application-side wrapping. Old SSE-only objects keep working
   until backfill catches up.
7. Phase 6: migrate `user_api_keys` from the env master key to the
   KMS hierarchy. This invalidates active rows (already documented
   as a known consequence of master-key rotation in
   `DEPLOYMENT-GUIDE.md` §"Secrets rotation"); coordinate a user
   notification window.

Total elapsed: weeks, not days. Each phase is independently
reversible up to phase 4.

## 8. Operational concerns

### KMS availability

The backend cannot decrypt anything if Vault is unreachable. Two
mitigations:

- Cache unwrapped tenant KEKs with a TTL (proposed 5 min idle, 1 h
  hard cap). Survives short Vault outages.
- Run Vault HA (3 nodes, Raft storage) inside the same cluster.
  Vault outage is then equivalent to cluster outage, no new failure
  mode.

### KEK rotation

Vault Transit supports key versions natively. Rotating the tenant
KEK creates a new version; old wrapped DEKs continue to unwrap under
the old version (Vault stores all). A background re-wrap worker
walks `patient_encryption_keys` in chunks and re-wraps under the
new version, updating `kek_version`. This is an O(patients)
operation but does not touch ciphertext.

### Backups

- `pg_dump` of `patient_encryption_keys` is now itself a sensitive
  artifact: it contains every wrapped DEK. Not catastrophic (still
  needs the KEK to unwrap) but should be treated as such. Encrypt
  backups with a key rooted in the same KMS, separate from the
  tenant KEK.
- Vault backups: snapshot the Raft storage encrypted. The KMS
  recovery key is the most sensitive secret in the deployment.
  Store it offline, ideally Shamir-split among multiple operators.

### Disaster recovery

The disaster recovery story changes shape. Today, restoring Postgres
+ S3 from backup yields a working system. With envelope encryption,
restoring those two also requires a working Vault + intact root KEK.
A loss of the root KEK is equivalent to **total data loss for all
encrypted columns and objects**, regardless of whether the ciphertext
is intact. This is an *intentional* property (it is what makes
crypto-shredding work) but must be reflected in the runbook.

### Performance

Rough order-of-magnitude estimates, to be measured:

- Per-request DEK unwrap from backend memory cache: microseconds.
- Per-request KEK unwrap from Vault on cache miss: ~1 to 5 ms.
- AES-GCM throughput on modern x86 with AES-NI: > 1 GB/s per core.
  Encrypting a 50 MB DICOM is sub-100 ms of CPU.
- DICOM upload path becomes CPU-bound where it was previously
  network-bound. Worker pool sizing may need revisiting.

## 9. Open questions, to resolve before implementation

These are *not* blockers for adopting the design, but each forces a
concrete decision that the document above leaves open.

1. **KMS choice**: HashiCorp Vault (self-hosted in-cluster)
   vs Scaleway KMS (managed but coupled to one cloud) vs cross-cloud
   (AWS KMS via HTTPS from Scaleway). Vault is the most portable;
   Scaleway KMS is the lowest-ops; cross-cloud adds latency and a
   second vendor relationship.
2. **Tenant model**: today phoenix is logically single-tenant. The
   tenant KEK layer is overhead in that case. Decision: keep the
   layer in the design but collapse it (tenant KEK == root KEK) until
   a real multi-tenant requirement appears. Cost: the schema needs a
   `kek_id` column from day one to avoid a future migration.
3. **Blind indexes for codice fiscale lookup**: HMAC of CF under a
   site-wide key, stored as a separate column, indexed. This permits
   exact-match patient lookup without unwrapping the DEK. Open
   question: does the site-wide HMAC key sit in env, in Vault, or in
   the tenant KEK hierarchy. The first two are simpler; the third is
   more consistent.
4. **Sharded vs per-patient DEKs for very large patients**: a single
   patient with thousands of studies still has one DEK. Acceptable.
   If audit requirements ever demand "key was used to decrypt N
   times", per-study sub-keys derived from the patient DEK via HKDF
   are a future extension.
5. **Worker access**: workers (`bvworkers`) need DEK access for
   thumbnail generation, fascicolo export, etc. They must hold the
   same Vault token as the backend, scoped identically. Workers
   running on a separate node pool or with looser network policy is
   a regression in the threat model and must be avoided.
6. **OpenData publish path**: the publish flow already de-identifies
   text and DICOM headers. Should it additionally use a *separate*
   ephemeral DEK for the published clone, destroyed on retraction?
   This would make retraction a crypto-shred operation, simpler than
   a "delete from S3 and pray it's not still in someone's CDN cache".
7. **Restore-into-staging**: how does an operator restore a production
   Postgres dump into a staging environment for debugging without
   moving the production tenant KEK there? Either staging gets a
   separate (read-only) Vault binding to production keys, or staging
   restores cannot decrypt anything. The latter is safer; the former
   is more useful. Likely answer: staging restores load only the
   schema and de-identified fixture data, never the wrapped DEKs.

## 10. Relationship to existing docs

- `security-encryption-deidentify-cors.md`: layer-2 controls (S3 SSE,
  DICOM header scrub). This document does not replace those; both
  remain in place.
- `security-gdpr.md`: defines the consent and erasure flow. §6 above
  is the cryptographic implementation of the erasure right.
- `security-rls.md`: row-level security continues to be the primary
  *authorisation* boundary. Envelope encryption is *confidentiality*,
  a complementary control. A user with RLS access to a patient also
  needs the backend to unwrap the DEK on their behalf; the two
  checks are independent and both must pass.
- `DEPLOYMENT-GUIDE.md`: §"Secrets rotation" already notes that
  rotating `BVP_BYOK_MASTER_KEY` invalidates `user_api_keys`. After
  Phase 6 of §7, that rotation property generalises to "rotating the
  root KEK in Vault re-wraps everything in the background, no row
  invalidation".
