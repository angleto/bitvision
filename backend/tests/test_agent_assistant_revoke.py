"""``revoked_at`` enforcement on the per-assistant secret auth path.

A leaked ``client_secret`` must be retirable independently of the
``is_active`` soft-pause flag. The auth resolver looks at both columns
so an operator who hits ``POST /api/ai-assistants/{id}/revoke``
immediately renders the plaintext credential inert, even if a future
PATCH flips ``is_active`` back to True without rotating.

These tests are pure / no-DB by design — the integration path lives
in the e2e suite. We pin two invariants:

  * source-level: the resolver's SELECT references ``revoked_at`` so a
    refactor cannot silently drop the filter;
  * model-level: ``AgentAssistant`` carries the ``revoked_at`` column
    (so the migration / model drift cannot mask the auth gate).
"""

from __future__ import annotations

import inspect

from sqlalchemy import inspect as sa_inspect

from bvphoenix.auth import deps as deps_module
from bvphoenix.db.models import AgentAssistant


def test_resolver_source_references_revoked_at_filter() -> None:
    """Pin the invariant at the source level so a refactor that drops
    the ``revoked_at`` filter from the SELECT fails CI even when no
    Postgres is reachable to run the integration cases."""
    src = inspect.getsource(deps_module._resolve_assistant_secret)
    assert "revoked_at.is_(None)" in src, (
        "the per-assistant secret resolver must reject revoked rows"
    )
    # Also check the legacy soft-pause flag is still part of the same
    # WHERE clause — defence in depth.
    assert "is_active.is_(True)" in src


def test_model_has_revoked_at_column() -> None:
    """Pin that the SQLAlchemy mapping declares ``revoked_at`` so the
    auth gate's filter compiles to a real column."""
    mapper = sa_inspect(AgentAssistant)
    column_names = {col.name for col in mapper.columns}
    assert "revoked_at" in column_names
    # Also pin the type: should be nullable timestamp.
    revoked_col = AgentAssistant.__table__.c.revoked_at
    assert revoked_col.nullable is True
    assert (
        "datetime" in str(revoked_col.type).lower() or "timestamp" in str(revoked_col.type).lower()
    )
