"""`bvphoenix-embed-models` — CRUD for the embedding-model registry.

Ops-facing entry point for the "add a model without code change"
workflow. The Python service layer
(:mod:`bvphoenix.services.embedding_models`) is the authoritative API;
this CLI is a thin, transactional wrapper so an operator can register,
promote, or retire a backend from the shell.

Commands:

* ``register`` — insert a new row (active, *not* default-for-kind).
  Deploy the weights first, then ``activate`` to flip the default.
* ``list`` — print the active, non-deprecated registry.
* ``activate`` — mark a model active and optionally promote it to
  default-for-kind. Safe to re-run; idempotent.
* ``deprecate`` — retire a model with a mandatory ``--reason``. The
  row is kept so historical embeddings still resolve.
* ``set-routing`` — write a TEXT model's routing (arq task + pgvector
  store table[s]) into its ``model_metadata``, the fact the query path,
  the worker dual-write loop, the backfill CLI and the admin coverage
  API all resolve (migration 0023 seeded the shipped models).

Sessions are borrowed from the shared :func:`bvphoenix.db.session.get_session`
context manager with ``SERVICE_SUBJECT`` so the CLI bypasses RLS the
same way migrations and workers do.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import click
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import EmbeddingModel
from bvphoenix.db.session import SERVICE_SUBJECT, get_session
from bvphoenix.services import embedding_models as svc

T = TypeVar("T")


def _format_row(row: EmbeddingModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "kind": row.kind,
        "dim": row.dim,
        "provider": row.provider,
        "weights_uri": row.weights_uri,
        "is_active": row.is_active,
        "is_default_for_kind": row.is_default_for_kind,
        "deprecated_at": row.deprecated_at.isoformat() if row.deprecated_at else None,
        "metadata": row.model_metadata,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _run_with_session[T](action: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Open a service-subject session, run ``action``, commit on success.

    Service-layer errors inheriting :class:`svc.EmbeddingModelError` are
    mapped to a non-zero exit code with the message on stderr — keeps
    each command body focused on its own inputs. ``get_session`` rolls
    back automatically when an exception propagates out of the context.
    """

    async def _run() -> T:
        async with get_session(subject_id=SERVICE_SUBJECT) as db:
            return await action(db)

    try:
        return asyncio.run(_run())
    except svc.EmbeddingModelError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


def _require_uuid(id: str) -> None:
    try:
        uuid.UUID(id)
    except ValueError:
        click.echo(f"invalid id {id!r}: not a UUID", err=True)
        sys.exit(2)


@click.group()
def cli() -> None:
    """Manage the embedding-model registry."""


@cli.command("register")
@click.option("--name", required=True, help="Unique model name, e.g. biomedclip-v2.")
@click.option(
    "--kind",
    required=True,
    type=click.Choice(["image", "text", "multimodal"]),
    help="Modality the model produces embeddings for.",
)
@click.option("--dim", required=True, type=int, help="Vector dimension.")
@click.option(
    "--provider",
    required=True,
    help="Backend tag (biomedclip, sentence-transformers, openai, onnx, api, ...).",
)
@click.option(
    "--weights-uri",
    default=None,
    help="hf:org/name, s3://..., https://...; omit for API-backed providers.",
)
@click.option(
    "--metadata",
    default=None,
    help="Optional JSON blob stored alongside the row.",
)
def register(
    name: str,
    kind: str,
    dim: int,
    provider: str,
    weights_uri: str | None,
    metadata: str | None,
) -> None:
    """Register a new embedding model (active, not default-for-kind)."""
    meta_obj: dict[str, Any] = {}
    if metadata:
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError as exc:
            click.echo(f"invalid --metadata JSON: {exc}", err=True)
            sys.exit(2)
        if not isinstance(parsed, dict):
            click.echo("--metadata must decode to a JSON object", err=True)
            sys.exit(2)
        meta_obj = parsed

    async def _action(db: AsyncSession) -> EmbeddingModel:
        return await svc.register_model(
            db,
            name=name,
            kind=kind,
            dim=dim,
            provider=provider,
            weights_uri=weights_uri,
            metadata=meta_obj,
        )

    row = _run_with_session(_action)
    click.echo(json.dumps(_format_row(row), indent=2))


@cli.command("list")
@click.option(
    "--kind",
    type=click.Choice(["image", "text", "multimodal"]),
    default=None,
    help="Filter to a single kind.",
)
def list_models(kind: str | None) -> None:
    """List active, non-deprecated embedding models."""

    async def _action(db: AsyncSession) -> list[EmbeddingModel]:
        rows = await svc.list_active_models(db)
        if kind is not None:
            rows = [r for r in rows if r.kind == kind]
        return rows

    rows = _run_with_session(_action)
    click.echo(json.dumps([_format_row(r) for r in rows], indent=2))


@cli.command("activate")
@click.argument("id")
@click.option(
    "--default/--no-default",
    "is_default",
    default=False,
    help="Also promote this model to default-for-kind.",
)
def activate(id: str, is_default: bool) -> None:
    """Reactivate a model (and optionally promote it to default)."""
    _require_uuid(id)

    async def _action(db: AsyncSession) -> EmbeddingModel:
        return await svc.activate_model(db, id, is_default_for_kind=is_default)

    row = _run_with_session(_action)
    click.echo(json.dumps(_format_row(row), indent=2))


@cli.command("deprecate")
@click.argument("id")
@click.option("--reason", required=True, help="Audit note stored in metadata.")
def deprecate(id: str, reason: str) -> None:
    """Retire a model. Kept in the registry so historical vectors resolve."""
    _require_uuid(id)

    async def _action(db: AsyncSession) -> EmbeddingModel:
        return await svc.deprecate_model(db, id, reason=reason)

    row = _run_with_session(_action)
    click.echo(json.dumps(_format_row(row), indent=2))


@cli.command("set-routing")
@click.argument("id")
@click.option("--arq-task", required=True, help="Arq task that produces the vectors.")
@click.option("--store-table", required=True, help="pgvector store table for the dense vectors.")
@click.option(
    "--sparse-store-table",
    default=None,
    help="Optional auxiliary sparsevec store (BGE-M3-style lexical arm).",
)
@click.option(
    "--colbert-store-table",
    default=None,
    help="Optional auxiliary ColBERT token-vector store (late-interaction rerank).",
)
def set_routing(
    id: str,
    arq_task: str,
    store_table: str,
    sparse_store_table: str | None,
    colbert_store_table: str | None,
) -> None:
    """Write a text model's routing into its registry row.

    Values are validated as plain lowercase identifiers (they are
    interpolated into SQL by the consumers); the command refuses
    non-text rows.
    """
    _require_uuid(id)

    async def _action(db: AsyncSession) -> EmbeddingModel:
        try:
            return await svc.set_text_routing(
                db,
                id,
                arq_task=arq_task,
                store_table=store_table,
                sparse_store_table=sparse_store_table,
                colbert_store_table=colbert_store_table,
            )
        except ValueError as exc:
            click.echo(str(exc), err=True)
            sys.exit(2)

    row = _run_with_session(_action)
    click.echo(json.dumps(_format_row(row), indent=2))


if __name__ == "__main__":
    cli()
