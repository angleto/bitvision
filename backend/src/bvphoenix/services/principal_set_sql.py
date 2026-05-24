"""SQL definition of ``principal_set(uuid)``.

``principal_set(subject_uuid)`` returns every subject id that a principal
inherits authority from — the caller itself plus every organization /
group they are a member of (transitively, via the memberships edge
table). It is the SQL twin of
``bvphoenix.services.permissions.principal_set`` and is the only way the
Row-Level Security policies installed by migration
``0009_rls_policies`` can resolve "the caller and their groups" without
calling back into the app.

The function is ``STABLE`` (pure within a transaction) and marked
``SECURITY DEFINER`` so it can read ``memberships`` even when the
calling session's RLS policy would hide them — it owns the data it
returns and is the trust boundary for inheritance.

Import ``PRINCIPAL_SET_SQL`` from a migration and ``op.execute`` it;
re-use is deliberate so the SQL stays in one place and tests can assert
against the same source of truth.
"""

from __future__ import annotations

# Recursive CTE: seed with the caller, then walk `memberships` upwards
# until no new parents are found. The result always includes the
# ``'00000000-0000-0000-0000-000000000001'`` public subject (seeded in
# migration 0003) so anonymous and authenticated callers alike can
# receive grants that target the public principal.
PRINCIPAL_SET_SQL = """
CREATE OR REPLACE FUNCTION principal_set(subject_uuid uuid)
RETURNS TABLE(subject_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH RECURSIVE inherited(subject_id) AS (
        SELECT subject_uuid
        WHERE subject_uuid IS NOT NULL
        UNION
        SELECT m.parent_subject_id
        FROM memberships m
        JOIN inherited i ON m.subject_id = i.subject_id
    )
    SELECT subject_id FROM inherited
    UNION
    SELECT '00000000-0000-0000-0000-000000000001'::uuid
$$;
"""

DROP_PRINCIPAL_SET_SQL = "DROP FUNCTION IF EXISTS principal_set(uuid)"


# Helper used by RLS policies to resolve the current session's caller —
# reads ``app.current_subject_id`` (set by ``get_db``) and returns NULL
# for the sentinel ``'anonymous'`` / ``'service'`` strings so callers
# can use the result directly in comparisons.
CURRENT_SUBJECT_SQL = """
CREATE OR REPLACE FUNCTION app_current_subject()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN current_setting('app.current_subject_id', true) IS NULL THEN NULL
        WHEN current_setting('app.current_subject_id', true) IN ('', 'anonymous', 'service') THEN NULL
        ELSE current_setting('app.current_subject_id', true)::uuid
    END
$$;
"""

DROP_CURRENT_SUBJECT_SQL = "DROP FUNCTION IF EXISTS app_current_subject()"


# Returns TRUE when the session is running as the privileged service
# context (Arq workers, migrations, CLI). Policies OR against this to
# let workers bypass user-scoped checks without needing a separate DB
# role — keeps dev setups single-role.
IS_SERVICE_CONTEXT_SQL = """
CREATE OR REPLACE FUNCTION app_is_service()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(current_setting('app.current_subject_id', true), '') = 'service'
$$;
"""

DROP_IS_SERVICE_CONTEXT_SQL = "DROP FUNCTION IF EXISTS app_is_service()"
