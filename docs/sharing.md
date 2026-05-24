# Sharing — link-based access for radiologists and collaborators

This document specifies the complete sharing model for bitvision phoenix:
how a study owner grants access to colleagues, patients, or the public
through shareable links with configurable permissions, optional passwords,
and time limits. It extends the grant model described in
[authorization.md](./authorization.md) and is designed for the primary
use case of **radiologists exchanging DICOM studies for consultations**.

---

## 1. Design goals

1. **Zero-friction sharing**: a radiologist uploads a case, clicks
   "Share", copies a link, sends it to a colleague. No registration
   required for the recipient — they click the link and see the study.
2. **Granular control**: the sharer decides exactly what the recipient
   can do (view metadata, view images, annotate, download DICOM,
   request LLM analysis).
3. **Optional password protection**: for sensitive cases, the link can
   require a password before granting access.
4. **Time-limited access**: every share link has a configurable TTL
   (default 30 days for consultations). Expired links stop working
   instantly.
5. **Revocability**: the owner can revoke any share link at any time.
   Access stops immediately — no cached tokens survive revocation.
6. **Public publication**: a separate action from sharing. Making a study
   public gives everyone permanent read access without a link token.
7. **Audit trail**: every share creation, access, and revocation is
   logged in `audit_log`.
8. **Future-safe**: the model composes with organizations, groups, and
   the marketplace — a share link is a grant, and grants are the
   universal permission primitive.

---

## 2. Data model

### share_links table

A share link is a **public entry point** to a grant. The grant controls
what permissions the link gives; the share_link adds the URL token,
optional password, and usage tracking.

```
share_links (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  grant_id          UUID NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
  token             VARCHAR(64) UNIQUE NOT NULL,
  password_hash     TEXT NULL,
  label             TEXT NULL,
  max_uses          INT NULL,           -- NULL = unlimited
  use_count         INT NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

**Relationship to grants:**

- Every share link has exactly one grant.
- The grant's `grantee_subject_id` points to the special `public`
  subject (the synthetic principal for unauthenticated visitors).
- The grant's `permissions[]` array controls what the link allows.
- The grant's `valid_until` controls the link's TTL.
- Revoking the grant (setting `revoked_at`) instantly disables the link.
- Deleting the share_link does NOT revoke the grant (the owner may want
  to regenerate a new token for the same grant).

### Interaction with existing tables

- `grants.resource_kind = 'study'` (share links target studies; series-
  level sharing uses the same mechanism with `resource_kind = 'series'`).
- `grants.grantor_subject_id` = the study owner.
- `grants.grantee_subject_id` = `public` subject (see §3).
- `grants.conditions` can carry `{"requires_password": true}` as a flag
  for the UI, but the actual enforcement is the `share_links.password_hash`.

---

## 3. The `public` subject

The subjects table has a single row with `kind = 'public'`. This row
is created during the initial data seeding (migration or bootstrap).
It serves as the grantee for:

- Share links (anyone with the token, registered or not)
- Public studies (implicit grant to `public` for `{read:metadata,
  read:pixels, read:annotations}`)

This unifies the permission model: the `can()` function always queries
grants against the user's principal set, and for share-link access the
principal set includes `public`.

---

## 4. Sharing flow — step by step

### 4.1. Radiologist shares a case with a colleague

1. Dr. A uploads study S (or already has it).
2. Dr. A opens `/studies/{id}` → clicks **"Share"** button.
3. A dialog opens with:
   - **Permissions presets**:
     - *View only* → `[read:metadata, read:pixels, read:annotations]`
     - *View + annotate* → `[..., write:annotations, write:report]`
     - *View + download* → `[..., download:dicom, download:derivative]`
     - *Full access* → all except `delete`, `transfer:ownership`,
       `list:for_sale`
     - *Custom* → checkboxes for each verb
   - **Expiry**: dropdown (24h, 7 days, 30 days, 90 days, never)
   - **Password** (optional): text field
   - **Label** (optional): free text for the owner's reference
     (e.g. "Dr. Rossi, second opinion")
4. On submit:
   - Backend creates a grant + a share_link with a random 32-byte
     URL-safe token.
   - Returns the share URL: `https://host/shared/{token}`
5. Dr. A copies the URL and sends it (email, WhatsApp, secure message).

### 4.2. Colleague opens the link

1. Colleague clicks `https://host/shared/{token}`.
2. Frontend calls `GET /api/shared/{token}/info` → backend returns:
   - study title, modality, date (metadata only — no pixels yet)
   - whether a password is required
   - expiry date
   - permissions the link grants
3. If password required: frontend shows a password form. Colleague
   enters password → `POST /api/shared/{token}/verify` with password.
4. Backend verifies:
   - Token exists
   - Grant not revoked
   - Not expired
   - Password matches (if required)
   - max_uses not exceeded
5. On success: backend issues a short-lived JWT (1h) with:
   - `grant_id` in the payload
   - `share_token` in the payload
   - `permissions` from the grant
6. Colleague's browser stores this JWT → accesses the study viewer
   through the normal API endpoints, which now recognize the grant.

### 4.3. Owner manages share links

1. Owner opens `/studies/{id}/permissions` (or a panel in the study
   detail page).
2. Sees a list of all share links:
   - Label, permissions summary, created date, expiry, use count
   - Status: active / expired / revoked
3. Actions per link:
   - **Copy URL** (re-copy)
   - **Revoke** → sets `grants.revoked_at = now()`
   - **Edit** → change label, password, expiry, permissions
4. Below: a **"Make public"** toggle that sets `studies.is_public`.

### 4.4. Public publication (separate from sharing)

- "Publish" sets `studies.is_public = true`.
- Public studies are visible to everyone without any token or login.
- Public URL: `https://host/studies/{id}` — the normal study detail page.
- Public access gives `{read:metadata, read:pixels, read:annotations}`.
  Downloads, annotations, and LLM calls still require registration.
- The owner can unpublish at any time → `is_public = false`.

---

## 5. API endpoints

### Share link management (owner only)

```
POST   /api/studies/{id}/share
  Body: { permissions: string[], expires_in_hours: int|null,
          password: string|null, label: string|null, max_uses: int|null }
  Response: { share_link_id, token, url, grant_id, expires_at }

GET    /api/studies/{id}/shares
  Response: [ { id, token, label, permissions, expires_at, revoked,
                use_count, max_uses, created_at } ]

PATCH  /api/share-links/{id}
  Body: { label?, password?, expires_in_hours?, permissions? }

DELETE /api/share-links/{id}
  (revokes the underlying grant)
```

### Share link access (anyone with the token)

```
GET    /api/shared/{token}/info
  Response: { study_title, modality, study_date, requires_password,
              expires_at, permissions }
  (no pixels, no full metadata — just enough for the landing page)

POST   /api/shared/{token}/verify
  Body: { password: string }  (omit if no password required)
  Response: { access_token, expires_in }
  (short-lived JWT scoped to this grant)
```

### Publication (owner only)

```
POST   /api/studies/{id}/publish
POST   /api/studies/{id}/unpublish
```

---

## 6. JWT claims for share-link access

When a share link is verified, the issued JWT contains:

```json
{
  "sub": "public",
  "grant_id": "uuid",
  "share_token": "token-string",
  "permissions": ["read:metadata", "read:pixels", ...],
  "study_id": "uuid",
  "iat": 1234567890,
  "exp": 1234571490
}
```

The `optional_user` dependency in the auth layer recognizes this JWT
shape: if `sub == "public"` and `grant_id` is present, it constructs a
synthetic user context with the granted permissions. The `can()` function
checks these directly — no database grant lookup needed for the hot path.

---

## 7. Security considerations

1. **Token entropy**: 32 bytes from `secrets.token_urlsafe(32)` = 256
   bits. Unguessable.
2. **Password hashing**: bcrypt, same as user passwords.
3. **Short-lived access tokens**: 1h TTL for share-link JWTs. If the
   grant is revoked mid-session, the JWT still works for up to 1h —
   acceptable for consultation use cases; the grant check on
   sensitive operations (download, annotate) hits the DB and catches
   revocation in real time.
4. **Rate limiting on verify**: prevent brute-force password guessing.
   429 after 5 failed attempts per token per hour.
5. **No enumeration**: `/api/shared/{token}/info` returns 404 for
   invalid tokens — doesn't distinguish "doesn't exist" from "revoked".
6. **Audit**: every link creation, verify, and revocation writes to
   `audit_log` with the actor, action, and metadata (IP, user-agent).

---

## 8. Frontend routes

| Route | Purpose |
|---|---|
| `/studies/{id}` | Study detail (existing) — add "Share" button + permissions panel |
| `/shared/{token}` | Landing page for a share link — shows study info, password form if needed, then opens the viewer |
| `/studies/{id}/permissions` | Full permissions dashboard (owner only) |

---

## 9. Presets

The sharing dialog offers presets to simplify the most common workflows:

| Preset | Permissions | Typical use |
|---|---|---|
| View only | `read:metadata, read:pixels, read:annotations` | "Look at this case" |
| Consultation | `read:metadata, read:pixels, read:annotations, write:annotations, write:report` | "Please annotate and give your opinion" |
| Download | `read:metadata, read:pixels, read:annotations, download:dicom, download:derivative` | "Download the raw data for your analysis" |
| Full | All except `delete`, `transfer:ownership`, `list:for_sale`, `share:delegate` | "Do anything you need with this case" |
| Custom | User picks | Advanced users |

Default expiry per preset:
- View only: 7 days
- Consultation: 30 days
- Download: 24 hours
- Full: 30 days

---

## 10. Relationship to other authorization features

| Feature | Mechanism | Documented in |
|---|---|---|
| **Share links** (this doc) | grant + share_link token | sharing.md |
| **Direct grants** (user-to-user) | grant with grantee = specific user | authorization.md §2 |
| **Organization grants** | grant with grantee = org/group | authorization.md §3 |
| **Marketplace grants** | grant with `is_commercial = true` | authorization.md §6 |
| **Public access** | `studies.is_public = true` | authorization.md §4.4 |
| **Delegation** | child grants with `parent_grant_id` | authorization.md §2 |

Share links are the **simplest, most common sharing path** — optimized
for the "send a link to a colleague" workflow. The other mechanisms
cover structured, long-term, or commercial relationships.
