"""Embedding-model registry service.

Thin persistence layer over :class:`EmbeddingModel` that every caller
(indexer, search, CLI) should go through to pick a model. Keeping this
one function deep avoids the temptation to scatter ad-hoc
``SELECT ... WHERE name=`` queries across the codebase — adding a
backend is then a DB change, not a code change.

Concurrency note: ``activate_model`` flips the ``is_default_for_kind``
bit and must clear any other current default for the same kind in the
same transaction. The partial unique index from migration 0016 keeps
the invariant ("at most one active, non-deprecated default per kind")
even under concurrent admin writes — we do the clear-then-set dance so
the happy path does not trip the index.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import EmbeddingModel

_VALID_KINDS: frozenset[str] = frozenset({"image", "text", "multimodal"})


class EmbeddingModelError(Exception):
    """Base class for registry-service failures callers may want to catch."""


class EmbeddingModelNotFound(EmbeddingModelError):
    """Raised when a lookup by id or name finds no active, non-deprecated row."""


class NoDefaultEmbeddingModel(EmbeddingModelError):
    """Raised when a kind has no default configured — usually a seed/ops bug."""


class DuplicateEmbeddingModel(EmbeddingModelError):
    """Raised when ``register_model`` hits the unique-name constraint."""


def _validate_kind(kind: str) -> None:
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid kind {kind!r}; expected one of {sorted(_VALID_KINDS)}")


async def get_default_model(kind: str, db: AsyncSession) -> EmbeddingModel:
    """Return the active, non-deprecated default model for ``kind``.

    Raises :class:`NoDefaultEmbeddingModel` if the kind has no default —
    that indicates a misconfigured deployment (the seed should always
    leave exactly one default per image/text kind).
    """
    _validate_kind(kind)
    stmt = (
        select(EmbeddingModel)
        .where(
            EmbeddingModel.kind == kind,
            EmbeddingModel.is_default_for_kind.is_(True),
            EmbeddingModel.is_active.is_(True),
            EmbeddingModel.deprecated_at.is_(None),
        )
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NoDefaultEmbeddingModel(f"no default embedding model for kind {kind!r}")
    return row


async def list_active_models(db: AsyncSession) -> list[EmbeddingModel]:
    """List every active, non-deprecated model, ordered by (kind, name)."""
    stmt = (
        select(EmbeddingModel)
        .where(
            EmbeddingModel.is_active.is_(True),
            EmbeddingModel.deprecated_at.is_(None),
        )
        .order_by(EmbeddingModel.kind, EmbeddingModel.name)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_model(id: str | uuid.UUID, db: AsyncSession) -> EmbeddingModel:
    """Return a model by id. Raises :class:`EmbeddingModelNotFound` if missing.

    Intentionally returns deprecated rows too — historical embeddings
    reference them and the caller may legitimately need to resolve one.
    """
    model_id = id if isinstance(id, uuid.UUID) else uuid.UUID(id)
    stmt = select(EmbeddingModel).where(EmbeddingModel.id == model_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise EmbeddingModelNotFound(f"embedding model {id!s} not found")
    return row


async def get_model_by_name(name: str, db: AsyncSession) -> EmbeddingModel:
    """Return a model by its unique ``name`` column (convenience helper)."""
    stmt = select(EmbeddingModel).where(EmbeddingModel.name == name)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise EmbeddingModelNotFound(f"embedding model {name!r} not found")
    return row


async def register_model(
    db: AsyncSession,
    *,
    name: str,
    kind: str,
    dim: int,
    provider: str,
    weights_uri: str | None,
    metadata: dict[str, Any] | None = None,
) -> EmbeddingModel:
    """Insert a new model row. Does not commit — caller owns the transaction.

    The model is created active but *not* default-for-kind; promote it
    with :func:`activate_model` once the weights are in place. That keeps
    deploys safe: the row can land ahead of the artefact without
    diverting live traffic.
    """
    _validate_kind(kind)
    if dim <= 0:
        raise ValueError(f"dim must be positive, got {dim!r}")
    if not name.strip():
        raise ValueError("name cannot be empty")
    if not provider.strip():
        raise ValueError("provider cannot be empty")

    row = EmbeddingModel(
        name=name,
        kind=kind,
        dim=dim,
        provider=provider,
        weights_uri=weights_uri,
        is_active=True,
        is_default_for_kind=False,
        model_metadata=metadata or {},
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise DuplicateEmbeddingModel(f"embedding model {name!r} already exists") from exc
    return row


async def activate_model(
    db: AsyncSession,
    id: str | uuid.UUID,
    *,
    is_default_for_kind: bool,
) -> EmbeddingModel:
    """Reactivate a model and optionally promote it to default-for-kind.

    Setting ``is_default_for_kind=True`` first clears the bit on any
    sibling row of the same kind (in one statement) so the partial
    unique index is not violated mid-transaction.
    """
    row = await get_model(id, db)
    if row.deprecated_at is not None:
        # Reactivation after deprecation is allowed — lift the flag so
        # the row is a first-class citizen again. Historical embeddings
        # continue to resolve either way.
        row.deprecated_at = None
    row.is_active = True

    if is_default_for_kind:
        # Clear the flag on every sibling of the same kind before
        # promoting this one. The partial unique index only protects
        # against *concurrent* conflicts; doing the clear in SQL keeps
        # the whole thing a single round trip.
        await db.execute(
            update(EmbeddingModel)
            .where(
                EmbeddingModel.kind == row.kind,
                EmbeddingModel.id != row.id,
                EmbeddingModel.is_default_for_kind.is_(True),
            )
            .values(is_default_for_kind=False)
        )
        row.is_default_for_kind = True
    else:
        row.is_default_for_kind = False

    await db.flush()
    return row


async def deprecate_model(
    db: AsyncSession,
    id: str | uuid.UUID,
    *,
    reason: str,
) -> EmbeddingModel:
    """Mark a model deprecated. Clears the default flag so its kind can
    be repromoted without a conflict.

    Rows are never deleted: historical ``embeddings`` rows keep
    referencing the name and need the registry entry to stay resolvable
    for rebuild / provenance workflows.
    """
    if not reason.strip():
        raise ValueError("reason cannot be empty — deprecations are audited")

    row = await get_model(id, db)
    row.is_active = False
    row.is_default_for_kind = False
    row.deprecated_at = datetime.now(UTC)
    # Preserve any prior metadata, just add/overwrite the deprecation
    # note. SQLAlchemy needs a fresh dict to detect the JSONB change.
    meta = dict(row.model_metadata or {})
    meta["deprecation_reason"] = reason
    row.model_metadata = meta
    await db.flush()
    return row
