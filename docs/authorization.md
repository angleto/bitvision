# Ownership, sharing, and permissions

This document specifies how bitvision phoenix controls who can see and do what
with DICOM studies, series, annotations, and derived datasets. It covers the
core authorization model, the revocable sharing flows, the multi-organization
scoping, the marketplace for paid data sharing, and the concrete data model
in PostgreSQL.

The design is driven by the principle **ownership + capability-based ACL +
organization scoping**, built for maximum flexibility and reversibility.

---

## 1. Actors and concepts

### Principals — anyone who can hold a permission

- **User** — an individual account (patient, doctor, researcher, admin).
- **Organization** — a named group with an external identity (a hospital, a
  research group, a radiology clinic, a university).
- **Group** — a scoped sub-set of an organization (e.g. "Oncology radiologists
  at Hospital X"). Grants can target a group instead of each user inside.
- **Public** — a synthetic principal representing unauthenticated visitors.
  Used exclusively for public datasets and demo content.

All principals share a common `subjects` table so that any grant can reference
any principal uniformly.

### Resources — what permissions apply to

- **Study** — primary unit of ownership. All practical sharing happens here.
- **Series** — sub-resource of a study; can be shared individually (e.g.
  share only the MR T2 series, not the whole exam).
- **Instance** — rare, but supported (share a single key slice).
- **Annotation** — can be shared independently of the underlying image (e.g.
  publish annotations under a different license).
- **Dataset** — a named collection of studies/series (e.g. "Liver CTs from
  2024") — used for marketplace listings and bulk research grants.

### Permission verbs — granular, composable

| Verb                   | What it allows                                                        |
|------------------------|------------------------------------------------------------------------|
| `read:metadata`        | See title, patient info (per privacy rules), dates, modality           |
| `read:pixels`          | See the actual images (via viewer or thumbnail)                        |
| `read:annotations`     | Read existing annotations and reports                                  |
| `write:annotations`    | Create / edit annotations (human)                                      |
| `write:report`         | Write or edit a textual report                                         |
| `run:llm`              | Run LLM jobs on the resource (cost billed to the grantee)              |
| `download:dicom`       | Download original `.dcm` files                                         |
| `download:derivative`  | Download packed volume (NIfTI) / thumbnails                            |
| `share`                | Grant a subset of their own permissions to other principals            |
| `share:delegate`       | Grant `share` itself — i.e. further delegation is allowed              |
| `publish`              | Make the resource public (read-only to everyone)                       |
| `list:for_sale`        | Create a marketplace listing (owner-only by default)                   |
| `transfer:ownership`   | Hand ownership to another principal (owner-only by default)            |
| `delete`               | Delete the resource (owner-only by default)                            |
| `commercial:use`       | Use the data for commercial purposes per license terms (marketplace)   |

Permissions compose. A doctor doing a consultation typically receives
`{read:metadata, read:pixels, read:annotations, write:annotations}`. A
researcher buying a dataset receives `{read:*, download:*, commercial:use}`.

### Ownership model

Every resource has:

- **`owner_subject_id`** — single individual principal who ultimately controls
  the resource (patient, doctor, researcher, or organization).
- **`owner_org_id` (nullable)** — an optional co-owning organization. When a
  doctor uploads a study for a patient at Hospital X, both the patient and
  Hospital X are owners. Conflicts resolve with explicit rules (see §5).
- **`is_public`** — boolean flag for public exposure.
- **`is_listed_for_sale`** — boolean flag surfacing marketplace state.

Only the individual owner (or co-owner org admin) can transfer ownership,
delete, or change public/sale flags.

#### PLATFORM_OWNER (OpenData)

A reserved sentinel subject (`subjects.id` configurable via
`BVP_PLATFORM_OWNER_SUBJECT_ID`, default
`00000000-0000-0000-0000-000000000099`, seeded by migration
`0036_platform_owner_subject`) owns every fascicolo of the OpenData
public dataset. Treat it as a service identity that never logs in.

Ownership semantics for PLATFORM_OWNER-owned resources:
- **read-only for every authenticated user**: visible via
  `visible_*_filter` and granted `PUBLIC_READ_PERMS` automatically,
  no explicit grant required;
- **write requires platform-admin authority**: only callers with
  `subject_id == PLATFORM_OWNER` (or `is_admin = true`) get
  `ALL_PERMS`; for everyone else, write actions on these resources
  are denied at the application layer (`effective_permissions_*`)
  and via RLS `WITH CHECK` clauses.

The publish flow (F12.4) clones a private fascicolo into a fresh
PLATFORM_OWNER-owned fascicolo after de-identification, so the
private record stays untouched and the public copy has its own
lifecycle (including independent erasure).

---

## 2. Grants — the atomic unit of sharing

A **grant** is a single record that says:

> "**Grantor** gives **Grantee** the permissions **P** over **Resource R**,
> starting at **T1**, until **T2** (or forever), under conditions **C**,
> revocable unless commercial."

```
grants (
  id                  UUID PRIMARY KEY,
  resource_kind       TEXT,           -- 'study' | 'series' | 'instance' | 'annotation' | 'dataset'
  resource_id         UUID,
  grantee_subject_id  UUID REFERENCES subjects,
  grantor_subject_id  UUID REFERENCES subjects,
  parent_grant_id     UUID NULL REFERENCES grants(id),   -- cascade-revoke link
  permissions         TEXT[] NOT NULL,                    -- ['read:pixels','write:annotations',...]
  conditions          JSONB NOT NULL DEFAULT '{}',        -- see below
  valid_from          TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until         TIMESTAMPTZ NULL,                   -- NULL = no expiry
  revoked_at          TIMESTAMPTZ NULL,
  revoked_by          UUID NULL REFERENCES subjects,
  is_commercial       BOOLEAN NOT NULL DEFAULT false,     -- marketplace grant → not revocable
  purpose             TEXT NULL,                          -- free-text reason
  created_at          TIMESTAMPTZ DEFAULT now()
)
```

**Conditions (JSONB)** — reusable constraint schema, examples:

- `{"anonymized": true}` — grantee sees the image with PHI removed
- `{"watermark": "Prof. Rossi, UniXY"}` — rendered watermark
- `{"no_download": true}` — block downloads even if permission implies it
- `{"rate_limit_per_hour": 100}` — throttle
- `{"ip_allowlist": ["10.0.0.0/8"]}` — network scoping
- `{"purpose_hint": "second opinion"}` — for audit / display

### Revocation — everything is reversible (almost)

- Any grantor can revoke any grant they issued, at any time, instantly.
- Revocation cascades: if grant G is revoked, all grants whose
  `parent_grant_id = G.id` are revoked transitively.
- **Exception: `is_commercial = true` grants cannot be revoked** unilaterally.
  A commercial grant was paid for — revocation requires a refund flow and
  buyer consent. This is the only departure from the "all reversible" rule,
  and it's unavoidable (contracts).
- Revoked grants are not deleted — they remain in the audit trail.

### Delegation

- To re-share a resource, the grantee needs `share` in their own grant.
- To allow the re-share to in turn be re-shared, they need `share:delegate`.
- The child grant is recorded with `parent_grant_id` linking to the parent →
  enables both cascade-revoke and provenance tracing.
- Permissions of a child grant **must be a subset** of the parent's
  permissions (enforced by trigger / application logic).
- Validity of a child grant **must not exceed** the parent's `valid_until`.

### Auto-grants (computed, not stored)

Some grants are implicit and computed at query time rather than inserted:

- Owner has all permissions on their resources.
- Co-owner org admins have all permissions, scoped to org context.
- Public resources generate an implicit `{read:metadata, read:pixels,
  read:annotations}` grant to the `public` subject.
- Group membership propagates: a grant to a group counts for every member
  while they remain in the group.

---

## 3. Organizations and multi-org access

- A study can be assigned to an organization as **primary org**
  (`owner_org_id`). This does not prevent other orgs from receiving grants
  in parallel.
- Membership table `memberships(subject_id, parent_subject_id, role)`
  supports `(user, org, role)` and `(user, group, role)` and
  `(group, org, 'nested')` edges.
- Roles inside an org: `admin`, `member`, `viewer` (org-level admin-like
  verbs, not data-level).
- An org's admin can issue org-level grants on resources co-owned by the org.
- Users have an **active org context** in their session (like GitHub's
  active org). Switching context changes the default grantor for new
  grants they issue.

**Concurrent grants example:** Patient P uploads a study, owned by P. P
issues a grant to Hospital X (primary org for the patient). X's admin grants
a sub-grant to group "Onco-radiologists". Separately, P grants the same
study to researcher R at University Y for a research project. All grants are
independent; revoking one does not touch the others.

---

## 4. Use-case walkthroughs

### 4.1. Patient asks for a second opinion from a doctor

1. Patient P owns study S.
2. P opens the "Request consultation" UI → selects Doctor M (from directory,
   by email, or by invite link).
3. Default grant preset: `{read:metadata, read:pixels, read:annotations,
   write:annotations}`, `valid_until = now() + 30 days`, `purpose = "second
   opinion"`, `share:delegate = OFF` by default.
4. Doctor M receives a notification → accepts → grant becomes active.
5. M annotates / writes findings → P is notified.
6. P closes consultation → grant is revoked → M loses access immediately.
7. Audit log: P sees exactly when M accessed, what they wrote, when access
   ended.

### 4.2. Doctor asks a colleague for a sub-consultation

- **If the original grant has `share:delegate`**: M creates a child grant
  to M2 (permissions ⊆ M's, TTL ≤ M's). M2 sees the study in their inbox.
  If P revokes M's grant, M2's child grant is auto-revoked.
- **If `share:delegate` is off**: M cannot extend. M must ask P to issue
  a parallel grant to M2 directly. This is the safer default: it makes
  every third-party access visible to the patient.

### 4.3. Doctor uploads for a patient under Hospital X

- Doctor uploads study S with `owner_subject_id = Patient P`,
  `owner_org_id = Hospital X`.
- Hospital X's internal group "Oncology radiologists" automatically receives
  a grant (policy-based: "all studies co-owned by X and tagged `oncology`
  are shared with group `onco-radiologists`").
- P can view and revoke at any time.

### 4.4. Anonymous visitor browses public data

- Resource S has `is_public = true`.
- Visitor is treated as the `public` subject.
- They get `{read:metadata, read:pixels, read:annotations}` — no
  `download:dicom`, no `run:llm`, no `write:*`.
- Rate limited per-IP.

### 4.5. Researcher buys a dataset from the marketplace

- Owner creates a `sale_listing`: dataset D, price 500€, license
  "commercial research, attribution required, no redistribution".
- Buyer B purchases via Stripe Checkout → a `purchase` record is created.
- Platform atomically issues a `grants` row: grantee = B, resource = D,
  permissions = `{read:metadata, read:pixels, read:annotations,
  download:dicom, commercial:use}`, `is_commercial = true`,
  `conditions = {"license": "commercial-research-v1", "attribution":
  "Seller X"}`.
- This grant is NOT revocable by the seller (see §2).
- Seller receives payout (minus platform fee — see §6 open questions).

---

## 5. Authorization decision flow

When user U requests action A on resource R:

1. **Build the principal set** for U: `{U.id} ∪ {orgs U belongs to} ∪
   {groups U belongs to} ∪ ('public' if unauth)`.
2. **Query effective grants** on R: all grants where `grantee_subject_id ∈
   principal_set`, not revoked, `now() BETWEEN valid_from AND
   COALESCE(valid_until, 'infinity')`.
3. **Add implicit grants**: if U is owner or org co-owner, add full
   permissions. If R is public, add public read grants.
4. **Union the permission sets**.
5. **Check if A is allowed** — any single grant is enough.
6. **Apply conditions** — e.g. redact PHI if `anonymized: true`, enforce
   rate limits, check IP allowlist.
7. **Log to audit** — every non-public access is recorded.

### PostgreSQL Row-Level Security

- RLS policies on `studies`, `series`, etc., implement step 2+3 at the DB
  level, ensuring `SELECT` only returns rows the current subject can see.
- Fine-grained action checks (`write:annotations` before inserting an
  annotation) happen in application code with the same query.
- The app sets a session variable `SET app.current_subject_id = ...;
  SET app.active_org_id = ...;` on every connection from the pool.

### Conflict resolution

- Multiple applicable grants → **most permissive wins** for allow, but any
  explicit "deny" (negative grant, future extension) overrides. Start
  without deny grants; add if real need arises.
- Co-owner conflicts between patient and org: the patient's individual
  wishes always override. If a patient wants to revoke an org's access,
  they can — even if the org was the uploading party.

---

## 6. Marketplace — paid data sharing

Data monetization is a **first-class, opt-in** feature, orthogonal to the
"platform free, LLMs paid" model. Users who want to can list their data for
sale; buyers get commercial-use grants.

### Entities

```
sale_listings (
  id                  UUID PRIMARY KEY,
  seller_subject_id   UUID REFERENCES subjects,
  resource_kind       TEXT,             -- usually 'dataset'
  resource_id         UUID,
  title               TEXT,
  description         TEXT,
  price_cents         INT,
  currency            TEXT,
  license             TEXT,             -- 'cc-by-nc-4.0' | 'commercial-research-v1' | ...
  requires_anonymization BOOLEAN,       -- if true, buyer only gets anonymized version
  preview_sample_size INT,              -- how many items visible pre-purchase
  active              BOOLEAN
)

purchases (
  id                  UUID PRIMARY KEY,
  listing_id          UUID REFERENCES sale_listings,
  buyer_subject_id    UUID REFERENCES subjects,
  grant_id            UUID REFERENCES grants,   -- the commercial grant issued
  paid_cents          INT,
  paid_at             TIMESTAMPTZ,
  payment_ref         TEXT              -- Stripe session / payout ref
)
```

### Flow

1. Seller owns resource (dataset / study).
2. Seller creates a listing, picking license and price.
3. Buyer browses the marketplace (a separate surface, not a data-privacy
   surface).
4. Buyer checks out via Stripe. On `checkout.completed` webhook:
   - Platform creates the `purchases` record.
   - Platform issues the commercial grant (`is_commercial = true`).
   - Seller gets a payout (Stripe Connect). Current leading model: zero
     platform fee with processor costs passed to the buyer — see §8.
5. Buyer accesses data via the normal grant flow.

### Why commercial grants are irrevocable

- Commercial transactions require stability for the buyer.
- A refund-revoke flow exists as an explicit escape hatch (seller can
  initiate a refund → platform revokes the grant → buyer agrees or
  disputes). Outside the ordinary revoke path.

### Protecting against leakage

- Technical protection is limited — any buyer with `download:dicom` can
  copy files offline. Mitigation:
  - Default to "streaming only" listings (no download) where possible.
  - Watermarks with buyer identity on rendered views.
  - Contractual terms in the license with audit rights.
- The system should not promise more than it can technically deliver.

---

## 7. Audit and transparency

- `audit_log` table captures every non-trivial action: views, downloads,
  LLM runs, shares, revocations, ownership transfers.
- Owners have an "Access history" view for each resource: who looked, when,
  from where, with what grant.
- Subjects accessing data always see a persistent "you are accessing X's
  data under grant Y, valid until Z, purpose P" banner — makes the
  permission chain visible.

---

## 8. Decisions and open questions

### Decided

- **`share:delegate` default = OFF**. New grants never allow further
  re-sharing unless the grantor explicitly turns delegation on. Keeps the
  patient / owner in control and makes every external access traceable
  back to an explicit decision. UI surfaces a one-click toggle to enable
  when appropriate (e.g. tumor board workflows).
- **Anonymization for external grants default = ON**. Any grant to a
  principal that is *not* in the owner's orgs (or not the owner
  themselves) ships with the DICOM-scrub flag on unless the grantor
  explicitly disables it. External actors see PHI-stripped data by
  default; de-anonymization is an explicit opt-in step with extra
  confirmation in the UI.
  - Implementation lives on the dedicated `grants.deidentify` boolean
    column (not inside `conditions`), evaluated on every download
    through `services.deidentify.should_deidentify`. The external / not
    in owner's orgs classification + default-ON policy is enforced in
    `services/grants.py:resolve_deidentify_default`; the column's
    server-side default stays False so the rule stays at the service
    layer where the membership context is available.

### Open

1. **Marketplace fees** — open. The P2P dataset-sharing surface (F9,
   deferred) will need a fee policy; not yet decided in code.
2. **Negative / deny grants**: skip at first, bake in later if a real need
   arises. Start without them.
3. **Group-as-grantor**: only individuals can issue grants; system records
   `grantor_subject_id = user` and `grantor_acting_as_org_id = org` for
   provenance. Accountability stays individual, attribution stays org-level.
4. **Ownership transfer UX**: two-step (offer + accept). Safer, clearer,
   cheap to implement.
