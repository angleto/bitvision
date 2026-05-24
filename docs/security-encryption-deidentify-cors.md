# Security hardening: S3 SSE, DICOM de-identification, CORS / HSTS

This note covers three MEDIUM-severity fixes shipped together: encryption
at rest for object storage, a DICOM Basic-Profile de-identification
filter on share-link downloads, and hardened browser-facing headers.

## 1. S3 server-side encryption

Every `put_object` / `upload_fileobj` call in the backend and workers
now passes a standard SSE header. The mode is selected by one setting:

| `BVP_S3_ENCRYPTION` | Behaviour |
| --- | --- |
| `AES256` (default) | SSE-S3 — key managed by the storage provider |
| `aws:kms`          | SSE-KMS — key arn from `BVP_S3_KMS_KEY_ARN` |
| `none`             | No header (dev against legacy MinIO) |

Central helper: `bvphoenix.storage.default_put_extra_args(settings)` —
returns the right dict. `S3Storage` consumes it at construction and
splats it into every put; `S3Storage.put_extra_args()` re-exposes it for
callers that reach boto3 directly (workers).

### Caller audit

All writer call sites verified:

- `backend/src/bvphoenix/storage/s3.py` — `upload_file`, `upload_bytes`
- `workers/src/bvworkers/tasks/pack_volume.py` — direct `s3.put_object`,
  uses `settings.put_extra_args()`
- `backend/src/bvphoenix/cli/import_dicom.py`, `api/studies.py`,
  `api/patients.py`, `api/reports.py` — all route through `upload_file`
  / `upload_bytes`, so inherit encryption automatically.

### Provider notes

- **AWS S3**: AES256 is free; KMS adds per-request cost but gives
  per-key audit trail.
- **MinIO**: honours `ServerSideEncryption: AES256` when KES is
  configured; otherwise rejects with `NotImplemented`. Set
  `BVP_S3_ENCRYPTION=none` for single-node dev clusters without KES.
- **Cloudflare R2**: silently ignores the header and encrypts
  unconditionally — AES256 is the safe default.

## 2. DICOM de-identification on share links

`Grant` rows gain a `deidentify: bool` column (migration
`0005_grants_deidentify.py`). When set, the instance-download endpoint
scrubs PHI before responding.

### Profile applied

Subset of DICOM PS3.15 Basic Application Confidentiality Profile — see
`bvphoenix.services.deidentify` for the tag lists.

- **Pseudonymised** (replaced with `ANON-<12-hex>` derived from SHA-256
  of the plaintext): `PatientName`, `PatientID`, `ReferringPhysicianName`,
  `InstitutionName`, `InstitutionalDepartmentName`, `StationName`.
- **Blanked** (value cleared, tag preserved so viewers don't choke):
  `PatientBirthDate`, `PatientBirthTime`, `PatientSex`, `PatientAge`,
  `PatientWeight`, `PatientSize`, `AccessionNumber`, `StudyID`.
- **Removed** entirely: addresses, telephone numbers, free-text
  comments, physician names, device serial numbers, ethnic group,
  occupation, etc. (full list in `_REMOVE_TAGS`).

`PatientIdentityRemoved=YES` and
`DeidentificationMethod="bitvision phoenix Basic Profile"` are stamped
on the output per PS3.3 C.12.1.

### Threat model & scope

**In scope**: header-level PHI leakage through shared DICOMs.

**Out of scope (document, don't fix here)**:

- **Burned-in pixel annotations** — the profile explicitly does not
  touch pixel data. Studies known to carry burned-in PHI should have
  `BurnedInAnnotation=YES` and be rejected server-side before the share
  link is created.
- **Private tags** — vendor-specific tags may still carry identifiers.
  A future revision should drop all groups ≥ 0x0008 that aren't on an
  allow-list.
- **Structured reports / key-object selections** — text content inside
  SR documents is not scrubbed. Share-with-de-ID of SRs is currently
  best-avoided.

### Request flow

1. Owner calls `POST /studies/{id}/share` with `deidentify: true`.
2. API persists the grant with `deidentify=true` and mints a share
   link.
3. Recipient opens the link, `POST /shared/{token}/verify` issues a JWT
   that carries `grant_id`.
4. Recipient's download of `GET /instances/{id}/file` is served
   **inline** (not as a 307 redirect to S3) — the handler downloads
   the raw bytes, runs them through `deidentify_dicom_bytes`, and
   streams the scrubbed result with `X-Deidentified: true` and
   `Cache-Control: no-store`.

A presigned S3 URL would bypass the filter, so de-identified downloads
give up the redirect optimisation. Non-share-link downloads keep the
redirect path unchanged.

### Helper

```python
from bvphoenix.services.deidentify import deidentify_dicom_bytes, should_deidentify

if should_deidentify(grant):
    dcm = deidentify_dicom_bytes(dcm)
```

`should_deidentify(grant)` accepts a `Grant` ORM row, a `ShareLink`, or
a plain mapping — returns `False` when the object doesn't expose the
flag so accidental misuse defaults to **don't claim we scrubbed**.

## 2b. Free-text de-identification for OpenData publish

Distinct from the DICOM header scrub above: the F12.4 publish flow
(`POST /api/patients/{id}/publish`) clones a private fascicolo into an
OpenData clone, and the textual fields (`clinical_notes.body`,
`patients.notes`, `patients.allergies`) go through a regex baseline
in `bvphoenix.services.deid_text.redact_text` before the clone is
stored.

### Patterns covered (Italian)

| Kind                    | Coverage                                                                                           | Placeholder |
|-------------------------|----------------------------------------------------------------------------------------------------|-------------|
| `regex_codice_fiscale`  | 16-char layout `LLLLLLNNLNNLNNNL`, case-insensitive                                                | `[CF]`      |
| `regex_email`           | `localpart@domain` with TLD ≥ 2 chars                                                              | `[EMAIL]`   |
| `regex_phone`           | Italian mobile `3xx + 7 digits` and fixed `0xx + 6-8 digits`, optional `+39` prefix; **each digit may be preceded by ` `, `-`, or `.`** so `333 1234567`, `333 123 4567`, `333-123-4567`, `333.123.4567`, `+39 333.123.4567` all match | `[TEL]`     |
| `regex_date_precise`    | `DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD` (year-only forms intentionally retained)                  | `[DATE]`    |
| `regex_address`         | `via\|viale\|piazza\|piazzale\|corso\|largo\|vicolo\|strada` + word + civic number                 | `[ADDR]`    |

The phone regex was tightened in F12.7 — the previous form only
accepted a *single* separator, so `333 123 4567` (the most common
print format) leaked. The contract is pinned by
`tests/test_deid_text_italian.py::TestPhone` with two parametrised
matrices:

- `REDACTED` — formats that MUST be caught. A regression here is a
  PHI leak.
- `MISSED` — formats that are intentionally out of scope (foreign
  international numbers without `+39`). The LLM scrub
  (`?use_llm_scrub=true`, F12.4-llm) is the secondary defence.

When extending the regex, prefer flipping a `MISSED` entry to
`REDACTED` over inventing a new kind. Auditors search redactions by
kind via `redaction_events.redaction_kind`; the kind alphabet is part
of the public contract and is itself test-pinned
(`TestAuditInvariants::test_kind_alphabet_is_stable`).

### What is NOT covered by regex

- **Proper names** (people, hospitals, cities) — the F12.4-llm pass
  is the dedicated tool. The regex is intentionally conservative to
  avoid false-positive redaction of clinical terms.
- **Foreign-format phone numbers** — see `MISSED` table.
- **Free-form addresses with abbreviations** (`V.le`, `P.zza`) — also
  in the `MISSED` table; LLM scrub is the path forward.

### Audit trail

Every redaction emits a `redaction_events` row carrying the
`original_excerpt_hash` (sha256 of the plaintext) but **not** the
plaintext itself. The hash lets auditors prove "the same plaintext
was scrubbed twice" without retaining the data. The dataclass
`Redaction` has no plaintext field; this is structurally enforced
(`tests/test_deid_text_italian.py::TestAuditInvariants::test_redaction_objects_carry_hash_not_plaintext`).

## 3. CORS / HSTS / TrustedHost hardening

### CORS

`settings.cors_origins` (comma-separated) governs the `Access-Control-
Allow-Origin` list:

| `BVP_ENV`     | `BVP_CORS_ORIGINS`    | Result |
| ------------- | --------------------- | ------ |
| `development` | empty                 | `*`   |
| `development` | `https://foo.example` | `https://foo.example` |
| `production`  | empty                 | **No origin allowed** — CORS denies |
| `production`  | `https://app.example` | `https://app.example` |

Prod never falls back to `*` — an operator who forgets the variable
gets a loud CORS-failure in the browser instead of a silent wildcard.

### TrustedHostMiddleware

`settings.trusted_hosts` (comma-separated list of hostnames, wildcards
allowed per Starlette). When non-empty, Host-header-spoofed requests
are rejected. Empty ⇒ middleware not installed (dev convenience).

### HSTS and static security headers

`SecurityHeadersMiddleware` adds on every response:

- `X-Content-Type-Options: nosniff` — everywhere.
- `Referrer-Policy: strict-origin-when-cross-origin` — everywhere.
- `Strict-Transport-Security: max-age=<hsts_max_age>; includeSubDomains`
  — **production only**, default max-age 2 years (preload-eligible).

Dev is skipped because an HSTS record on a self-signed `localhost`
would trap the browser into requiring HTTPS on subsequent visits.

### Settings summary

```ini
BVP_ENV=production
BVP_CORS_ORIGINS=https://app.example.com,https://admin.example.com
BVP_TRUSTED_HOSTS=api.example.com,*.example.com
BVP_HSTS_MAX_AGE=63072000  # optional; 2 years default
BVP_S3_ENCRYPTION=AES256   # or aws:kms
BVP_S3_KMS_KEY_ARN=arn:aws:kms:eu-west-1:...  # only if aws:kms
```
