# PostgreSQL Row-Level Security (RLS)

> Status: enabled since migration `0009_rls_policies` (2026-04-17).
> Scope: `studies`, `patients`, `reports`, `annotations`,
> `patient_documents`, `grants`.

Authorization in bitvision phoenix has two layers:

1. **Application layer** — `bvphoenix.services.permissions`. Returns
   `403` on denied requests, builds filtered SQL queries, is the layer
   routes interact with directly.
2. **Database layer** — RLS policies on every resource table. This is
   the *trust boundary*. Even if a query is built incorrectly, the
   database will not return rows the caller is not entitled to.

Both layers implement the same predicates (see `authorization.md §5`).
Divergence between them is a bug — change both when a predicate
changes.

---

## How a request flows

```
HTTP
  │
  ▼
FastAPI dep graph:
  get_db      ─► opens AsyncSession
              ─► SET app.current_subject_id = 'anonymous'  (safe default)
  optional_user
              ─► decodes bearer token
              ─► SET app.current_subject_id = '<user uuid>' (on auth)
  route handler
              ─► queries go through RLS policies
              ─► app layer also calls `can(...)` / `visible_studies_filter(...)`
                 for 403 messages and index-friendly filtering
```

If a request never reaches `optional_user` (e.g. an unauthenticated
endpoint), the session stays on `'anonymous'` and policies restrict
access to `is_public` rows plus grants targeting the seeded public
subject (`00000000-0000-0000-0000-000000000001`).

---

## The three SQL helpers

Defined in `bvphoenix.services.principal_set_sql`, installed by the
migration.

| Function               | Returns | Purpose                                         |
|------------------------|---------|-------------------------------------------------|
| `principal_set(uuid)`  | `setof uuid` | Caller + every org / group they inherit from, plus the public subject. Recursive walk of `memberships`. |
| `app_current_subject()`| `uuid` (nullable) | Reads `app.current_subject_id`; returns NULL for `anonymous` / `service`. |
| `app_is_service()`     | `boolean` | True when the session is in the service-bypass context (Arq workers, CLI, migrations). |

`principal_set` is declared `SECURITY DEFINER` so it can read
`memberships` even when the caller's session would hide it — it is the
one component that needs bypass, and because it only returns
subject-id sets (no PHI) the exposure surface is limited.

---

## The bypass strategy

Services that run background work (Arq workers, CLI ingestion jobs,
bootstrap scripts) cannot be scoped to a user — they operate on the
whole corpus. Two escape hatches are offered, cheapest first:

### 1. `app.current_subject_id = 'service'` (recommended)

Every RLS policy starts with `app_is_service() OR …`, so setting the
session variable to the string `'service'` skips the user check. This
is what `get_session()` (the non-FastAPI context manager in
`db/session.py`) does by default.

Pros: single DB role, no privilege splitting, works in dev with MinIO
and a local Postgres.

Cons: anyone who can set a session variable can escape RLS. Only code
running with access to the app's DB credentials can do that, which is
already the full-trust boundary for the deployment.

### 2. Dedicated `bvp_service` role with `BYPASSRLS` (future)

For production, we plan to add:

```sql
CREATE ROLE bvp_service WITH LOGIN BYPASSRLS PASSWORD '…';
GRANT ALL ON SCHEMA public TO bvp_service;
```

and have the workers connect as `bvp_service` instead of the app role.
This removes the "anyone can set a variable" caveat at the cost of
maintaining a second credential set. It is **not** required for the
current deployment; adopt it when the credential pipeline is in place.

In the meantime, **never** use `SET row_security = off` in application
code — it's an easy footgun in code review. If you truly need the
bypass, call the context manager that sets the `'service'` subject
explicitly so the intent is visible at call sites.

---

## Policies at a glance

| Table              | SELECT visibility                                 | Writes                                   |
|--------------------|---------------------------------------------------|------------------------------------------|
| `studies`          | public OR owner OR org-owner OR active grant on study OR active grant on patient | owner |
| `patients`         | manager OR self OR active patient grant           | manager (create), manager/self (update)  |
| `reports`          | cascading through parent `studies` SELECT          | author OR study owner                    |
| `annotations`      | cascading through study/series/instance → `studies` | author OR study owner                   |
| `patient_documents`| cascading through parent `patients` SELECT         | uploader OR patient manager/self         |
| `grants`           | grantor OR grantee (in principal set) OR owner of referenced resource | grantor only                 |

Every policy has `app_is_service()` as its leftmost OR term so the
service bypass is cheap (planner short-circuits, no CTE evaluation).

---

## Adding RLS to a new table

Checklist when you create a new table that stores user-visible data:

1. Write the app-layer predicate first, in
   `bvphoenix.services.permissions` (or a sibling module). This is
   how routes will build queries.
2. In a new migration:
   - `ALTER TABLE … ENABLE ROW LEVEL SECURITY;` (do **not** force; the
     DB owner must keep bypass for DDL).
   - `CREATE POLICY …` for `SELECT`, `INSERT`, `UPDATE`, `DELETE`. The
     first OR clause of every `USING` / `WITH CHECK` must be
     `app_is_service()`.
   - Mirror the app-layer predicate exactly. Re-use
     `principal_set(app_current_subject())` for principal expansion;
     re-use `app_current_subject()` for the caller.
3. Add a test in `backend/tests/` that:
   - inserts two users A and B and a row owned by A,
   - issues a query as B,
   - asserts zero rows come back even without the app-layer filter.
4. Update this document with a row in the "Policies at a glance"
   table.

---

## Testing RLS locally

```sql
-- As the app role (NOT the superuser):
SET app.current_subject_id = '<user uuid>';
SELECT * FROM studies;          -- only user's + public + granted

SET app.current_subject_id = 'anonymous';
SELECT * FROM studies;          -- only is_public = true

SET app.current_subject_id = 'service';
SELECT * FROM studies;          -- everything
```

If queries return unexpected rows:

- Check that RLS is actually enabled:
  `SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = '…';`
- Check the policy list: `\d+ studies` in psql.
- Ensure you're not connected as the DB owner / a superuser — both
  bypass RLS by default. Use a non-owner role for verification.

---

## Known limitations

- `series`, `instances`, `derivatives`, `embeddings`, `tags`,
  `audit_log`, `folders`, `folder_items`, `share_links` are **not**
  covered by RLS yet. Their access is enforced app-layer only today.
  Follow the "Adding RLS to a new table" checklist when extending.
- The cascade predicates issue sub-queries against `studies` /
  `patients` which themselves have RLS. This is intentional — the
  visibility chain naturally follows ownership. It may cost an extra
  index lookup per row; benchmark before optimising.
- `principal_set` uses a recursive CTE over `memberships`. Deep
  nesting (>10 levels) will slow queries. Cap nesting in the app
  layer when onboarding orgs.

---

## Production hardening required: owner-bypass

PostgreSQL bypasses RLS for the **table owner** unless the table has
`FORCE ROW LEVEL SECURITY` enabled. Both `0009_rls_policies` and
`0035_versioning_schema` declare policies but do **not** force RLS, on
the assumption that the production app role is distinct from the
table-owner role.

**In dev (and any deploy that uses `bvphoenix` as both owner and app
role) every RLS policy is decorative.** A query that should be
filtered by RLS returns all rows, because the connected role owns the
table. The application-layer predicates in `services/permissions.py`
are still the effective gate, but the database-level "trust boundary"
promised in §1 does not actually exist.

The structural test
`tests/test_versioning_security.py::TestRlsStructuralEnforcement::test_audit_tables_protected_against_owner_bypass`
codifies the invariant. It is currently `xfail` in dev. To unblock it
in production pick one of:

### Option A — separate app role (recommended)

```sql
-- Run as a superuser:
CREATE ROLE bvp_app WITH LOGIN PASSWORD '…';
GRANT USAGE ON SCHEMA public TO bvp_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO bvp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bvp_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bvp_app;

-- Deployment connects FastAPI as bvp_app, Alembic stays as bvphoenix.
```

Pros: zero migration risk; matches the bypass model in §"The bypass
strategy" (the app role is **not** `BYPASSRLS`, only the dedicated
`bvp_service` role for workers is).
Cons: the deployment pipeline must manage a second credential.

### Option B — `FORCE ROW LEVEL SECURITY` on every sensitive table

A new migration would `ALTER TABLE … FORCE ROW LEVEL SECURITY` on every
table with policies. Migrations themselves run as the owner via
Alembic; once forced, they too become subject to RLS, so every DML
migration must `SELECT set_config('app.current_subject_id', 'service', true)`
at the top.

Pros: single DB role, no separate credential pipeline.
Cons: every future migration that does INSERT/UPDATE/DELETE on a
covered table must remember the `set_config` line, or it silently
inserts zero rows. Easy to break in a code review.

### Verifying which path is active

```sql
SELECT relname, relrowsecurity, relforcerowsecurity,
       (SELECT rolname FROM pg_roles WHERE oid = relowner) AS owner_role,
       current_user
FROM pg_class
WHERE relkind = 'r'
  AND relname IN ('patients','studies','grants','ref_log','commits',
                  'manifest_entries','entity_objects','proposals',
                  'audit_log');
```

A row is *protected* iff `relforcerowsecurity = true` OR
`owner_role <> current_user`. The xfail test above runs the same
query and lists every offender.
