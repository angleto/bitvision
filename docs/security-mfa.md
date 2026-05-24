# MFA / TOTP — design notes

bitvision phoenix is a medical-imaging platform; HIPAA-style deployments
treat admin accounts as high-value targets and demand a second factor.
This note describes the TOTP + backup-code flow shipped in unit **S5**.

## Threat model

- Password-only admin accounts are the single most attractive target on
  a healthcare deployment: an attacker with admin credentials can
  exfiltrate every study, every Health Record and every PHI report.
- bitvision phoenix uses local password + JWT as its default auth
  path, with optional external OIDC SSO for browser logins. Either
  way, admins must not be one stolen password away from full access.
- We therefore require TOTP as a second factor for admins, with backup
  codes to cover device loss. Regular users can opt in.

## Data model

Columns on `users` (migration `0008_mfa.py`):

| column | type | meaning |
|---|---|---|
| `mfa_secret` | `TEXT NULL` | Base32 TOTP secret. `NULL` → MFA never started. |
| `mfa_enabled_at` | `TIMESTAMPTZ NULL` | Set to `now()` at activation; `NULL` means either disabled or setup pending. |
| `backup_codes_hash` | `TEXT[] NULL` | Bcrypt hashes of the 10 one-shot recovery codes. |

**Production hardening.** `mfa_secret` is stored as plaintext `TEXT` in
the open-source default because adding a KMS dependency shouldn't be
mandatory to run the platform. Production deployments must:

1. Switch the column to `BYTEA` (new migration).
2. Wrap writes with a KMS data-encryption key (AWS KMS / GCP KMS /
   HashiCorp Vault / cloud-agnostic envelope).
3. Unwrap in `bvphoenix.api.mfa.verify_totp` / the login path only.

The `TEXT` shape was chosen so the first cut ships as *opt-in security*
improvement without bolting a KMS onto every dev laptop.

## State machine

```
 none ── /mfa/setup ──▶ pending ── /mfa/activate ──▶ enabled
  ▲                                                     │
  └─────────────────  /mfa/disable ─────────────────────┘
```

- `none`: `mfa_secret IS NULL AND mfa_enabled_at IS NULL`.
- `pending`: `mfa_secret IS NOT NULL AND mfa_enabled_at IS NULL` — the
  user generated a secret but never confirmed a code. Restarting setup
  clobbers the old secret.
- `enabled`: both columns set, backup codes issued.

## Endpoints

All endpoints live under `/api` and require a standard Bearer token
(from `/auth/login` or `/auth/login-mfa`).

| verb | path | purpose |
|---|---|---|
| `GET` | `/api/mfa/status` | Returns `{enabled, pending, enabled_at, backup_codes_remaining}`. |
| `POST` | `/api/mfa/setup` | Generates/refreshes the TOTP secret, returns provisioning URI + QR PNG (base64). Moves user to *pending*. Refused if already enabled. |
| `POST` | `/api/mfa/activate` | Body `{totp_code}`. Verifies the code, stamps `mfa_enabled_at`, returns 10 backup codes (shown once). |
| `POST` | `/api/mfa/disable` | Body `{totp_code}`. Requires a currently-valid TOTP **or** an unused backup code. Admins cannot disable while `BVP_REQUIRE_MFA_FOR_ADMIN` is on. |
| `POST` | `/api/auth/login-mfa` | Body `{email, password, totp_code}`. Combined login once MFA is active. |
| `POST` | `/api/auth/login` | Password-only; returns **401 `{detail: "mfa_required"}`** when the account has MFA enabled, **403 `{detail: "mfa_enrolment_required"}`** when admin + not enrolled. |

## Backup codes

- 8 uppercase alphanumeric characters (`A-Z0-9`). Easy to read back off
  a printed sheet without confusing O/0 or I/1 (we do include both, but
  the fixed-width display makes it unambiguous enough in practice).
- 10 codes issued at activation.
- Stored as bcrypt hashes; `backup_codes_hash` is a Postgres text array.
- Used atomically: verifying a backup code removes it from the array
  inside the same transaction as the authenticated request.

## Enforcement

- `BVP_REQUIRE_MFA_FOR_ADMIN` (default **true**) — plain
  `/auth/login` refuses admins that have not yet activated MFA.
- Admins cannot self-disable MFA while the flag is on. This prevents
  an attacker with a freshly-stolen admin session from dropping MFA.

### Bootstrap paradox

The strict admin-MFA policy creates a chicken-and-egg on a fresh
deployment: the first admin cannot log in to run `/mfa/setup`. Use the
CLI escape hatch:

```bash
# create the admin (no MFA yet)
bvphoenix-admin create-user --email admin@example.org \
  --password 'strong-pw' --display-name 'Admin' --admin

# mint a TOTP secret and mark MFA active in one shot — prints the
# otpauth:// URI to import into an authenticator app
bvphoenix-admin mfa-bootstrap --email admin@example.org

# now /api/auth/login-mfa works
```

## Frontend

`/settings/mfa` drives the whole flow:

1. Calls `/mfa/status` on load.
2. Not enabled → "Start setup" button → `/mfa/setup`, displays QR code
   + base32 secret.
3. User enters the code → `/mfa/activate`, backup codes rendered on
   screen with a "Download .txt" button. They're never fetched again.
4. Enabled → "Disable" form for non-admins.

The login page catches the `mfa_required` 401 from `/auth/login` and
reveals a second field for the TOTP code before calling
`/auth/login-mfa`. Backup codes are accepted at the login prompt as
well.

## Testing checklist

- [ ] `POST /mfa/setup` returns non-empty base32 + PNG.
- [ ] `POST /mfa/activate` with a code from the fresh secret succeeds
      exactly once; the same code the next minute fails.
- [ ] `POST /auth/login` returns 401 `mfa_required` once MFA is active.
- [ ] `POST /auth/login-mfa` with a valid TOTP returns a JWT.
- [ ] A consumed backup code can't be reused.
- [ ] Admin can't disable MFA while `BVP_REQUIRE_MFA_FOR_ADMIN=true`.
- [ ] `bvphoenix-admin mfa-bootstrap` activates MFA and prints a
      working provisioning URI.
