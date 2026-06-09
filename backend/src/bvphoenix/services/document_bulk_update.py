"""Bulk PATCH document service (Sprint 2, ADR 0003).

Apply N metadata edits to N patient documents in a single atomic-or-
not transaction. Each item carries the same shape as the single PATCH
endpoint (title / document_type / document_date / text); a per-item
``etag`` (If-Match) can be supplied to opt into optimistic concurrency
on the per-item granularity.

Two modes:

* **Atomic** (``atomic=True``): a single failure rolls back every item
  and the response is a 422 ``bulk_failed`` Problem-Details. Useful
  when the agent has computed the full manifest from a snapshot and
  needs all-or-nothing semantics.
* **Best-effort** (``atomic=False``, default per ADR 0003): each item
  succeeds or fails independently; the response is an array of
  per-item outcomes. The agent decides what to retry.

Dry-run replays the same validation pipeline but emits a per-item diff
without committing.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Document, DocumentFile, Patient, User
from bvphoenix.services.document_catalog_validation import (
    CatalogActiveIds,
    load_active_catalog_ids,
    validate_kind_id,
)
from bvphoenix.services.versioning import ActorContext, CommitResult


@dataclass(slots=True)
class BulkUpdateItem:
    """One pending edit. Mirrors :class:`PatientDocumentUpdateIn` plus
    the optional ``etag`` precondition.

    ``document_type`` is the legacy single-axis alias of ``kind_id``;
    accepted on the wire so existing MCP / FE clients keep working,
    but collapsed onto ``kind_id`` before apply because the underlying
    column was dropped in migration 0075. ``kind_id`` wins when both
    are supplied."""

    document_id: uuid.UUID
    title: str | None = None
    document_type: str | None = None
    kind_id: str | None = None
    document_date: date | None = None
    text: str | None = None
    etag: str | None = None
    # Sentinel: which keys the caller actually supplied. Lets us
    # distinguish ``text=None`` (clear) from "field absent".
    fields_set: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class BulkItemResult:
    document_id: uuid.UUID
    status: str  # 'ok' | 'error' | 'dry_run'
    diff: dict[str, dict[str, Any]] | None = None
    etag: str | None = None
    error: dict[str, Any] | None = None


@dataclass(slots=True)
class BulkResult:
    items: list[BulkItemResult]
    n_ok: int = 0
    n_error: int = 0
    n_dry_run: int = 0
    head_etag: str | None = None  # the patient main branch head after all commits

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "items": [asdict(i) for i in self.items],
            "n_ok": self.n_ok,
            "n_error": self.n_error,
            "n_dry_run": self.n_dry_run,
            "head_etag": self.head_etag,
        }


def _diff_doc(doc: Document, item: BulkUpdateItem) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    seen_keys: set[str] = set()
    for k in item.fields_set:
        if k == "etag":
            continue
        v = getattr(item, k)
        if k == "text" and v == "":
            v = None
        if k == "document_date" and v is not None:
            v = v.isoformat() if isinstance(v, (date, datetime)) else str(v)
        # Collapse the legacy ``document_type`` alias onto ``kind_id``
        # so the diff shows the column that actually moves; kind_id
        # wins on collision.
        target_key = "kind_id" if k == "document_type" else k
        if target_key in seen_keys:
            continue
        seen_keys.add(target_key)
        before = getattr(doc, target_key, None)
        if target_key == "document_date" and before is not None:
            before = before.isoformat()
        if before == v:
            continue
        out[target_key] = {"before": before, "after": v}
    return out


def _resolve_effective_kind(item: BulkUpdateItem) -> str | None:
    """Return the kind value the apply loop will write, or ``None``.

    Mirrors the collapse rules used by the apply loop further down:
    ``kind_id`` wins over the legacy ``document_type`` alias when both
    are supplied; either path produces a single value to validate.
    """
    if "kind_id" in item.fields_set and item.kind_id is not None:
        return item.kind_id
    if "document_type" in item.fields_set and item.document_type is not None:
        return item.document_type
    return None


def _validate_kind(item: BulkUpdateItem, catalog: CatalogActiveIds | None = None) -> str | None:
    """Validate ``item.kind_id`` / ``document_type``.

    Empty string remains a 422 (unchanged). When ``catalog`` is
    supplied, also reject values that are not present in the active
    ``document_kinds`` rows — the same pre-validation the single-doc
    PATCH does. Without ``catalog`` the FK violation only surfaces
    later, on flush, and the API layer translates it to a 422.
    """
    if "kind_id" in item.fields_set and item.kind_id is not None and not item.kind_id.strip():
        return "kind_id cannot be empty"
    if (
        "document_type" in item.fields_set
        and item.document_type is not None
        and not item.document_type.strip()
    ):
        return "document_type (alias of kind_id) cannot be empty"
    if catalog is not None:
        kind_err = validate_kind_id(_resolve_effective_kind(item), catalog)
        if kind_err is not None:
            return kind_err
    return None


async def apply_bulk_update(
    db: AsyncSession,
    *,
    patient: Patient,
    user: User,
    request: Any,
    items: list[BulkUpdateItem],
    atomic: bool,
    dry_run: bool,
    actor_override: ActorContext | None = None,
) -> BulkResult:
    """Run the bulk update pipeline.

    ``actor_override`` lets a caller attribute the commit to a specific
    actor instead of deriving it from ``request.state`` — used by the
    embedded Q&A assistant so a metadata edit it performs on the user's
    behalf is recorded with ``author_kind='agent'`` (AI provenance must
    stay visible) rather than as a plain human edit.

    The caller is responsible for the surrounding HTTP gates (auth,
    permission). This function owns the validation + per-item commit
    cycle and leaves the DB transaction open: the endpoint either
    ``commit`` (atomic on success) or per-item ``commit`` (best-effort)
    is responsibility of the caller. A ``dry_run`` invocation never
    mutates the DB.
    """
    from bvphoenix.api.patients import _document_versioning_payload  # avoid circular imports
    from bvphoenix.services.etag import etag_for_branch
    from bvphoenix.services.versioning_hooks import record_versioned_change

    out_items: list[BulkItemResult] = []
    n_ok = n_error = n_dry = 0

    # Pre-load every document referenced by the manifest in a single
    # query to keep latency bounded.
    doc_ids = [i.document_id for i in items]
    docs = (
        (
            await db.execute(
                select(Document).where(
                    Document.id.in_(doc_ids),
                    Document.patient_id == patient.id,
                )
            )
        )
        .scalars()
        .all()
    )
    docs_by_id = {d.id: d for d in docs}

    head_etag = await etag_for_branch(db, patient_id=patient.id, ref_name="main")

    # Snapshot the active catalog once per request so per-item kind
    # validation is O(1) and the bad-kind path returns a structured
    # 422 instead of bubbling up as an IntegrityError on flush.
    catalog = await load_active_catalog_ids(db)

    for item in items:
        doc = docs_by_id.get(item.document_id)
        if doc is None:
            out_items.append(
                BulkItemResult(
                    document_id=item.document_id,
                    status="error",
                    error={
                        "type": "not_found",
                        "detail": "document not found or not part of this patient",
                    },
                )
            )
            n_error += 1
            if atomic:
                break
            continue

        kind_err = _validate_kind(item, catalog)
        if kind_err is not None:
            out_items.append(
                BulkItemResult(
                    document_id=item.document_id,
                    status="error",
                    error={"type": "invalid_document_type", "detail": kind_err},
                )
            )
            n_error += 1
            if atomic:
                break
            continue

        # Per-item ETag: aligned with the single-doc PATCH endpoint
        # (``api/patients.py::update_document``). The bundle / GET
        # exposes ``document.etag`` as a per-row UUID rotated on every
        # mutation; this is the value the caller is expected to echo
        # back. ``"*"`` is the RFC 9110 wildcard "any current
        # representation" — explicit opt-out of optimistic concurrency.
        if item.etag is not None:
            current_doc_etag = str(doc.etag) if doc.etag is not None else None
            presented = item.etag.strip().strip('"')
            if presented != "*" and presented != current_doc_etag:
                out_items.append(
                    BulkItemResult(
                        document_id=item.document_id,
                        status="error",
                        error={
                            "type": "etag_mismatch",
                            "detail": (
                                f"item etag {presented!r} stale vs current {current_doc_etag!r}"
                            ),
                            "current_etag": current_doc_etag,
                        },
                    )
                )
                n_error += 1
                if atomic:
                    break
                continue

        diff = _diff_doc(doc, item)

        if dry_run:
            out_items.append(
                BulkItemResult(
                    document_id=item.document_id,
                    status="dry_run",
                    diff=diff,
                    etag=head_etag,
                )
            )
            n_dry += 1
            continue

        if not diff:
            # No-op: skip but report success.
            out_items.append(
                BulkItemResult(
                    document_id=item.document_id,
                    status="ok",
                    diff={},
                    etag=head_etag,
                )
            )
            n_ok += 1
            continue

        # Build the effective field map: collapse ``document_type``
        # onto ``kind_id`` (the column the legacy alias points at) so
        # the alias actually persists. ``kind_id`` wins on collision.
        effective: dict[str, Any] = {}
        for k in item.fields_set:
            if k == "etag":
                continue
            v = getattr(item, k)
            if k == "text" and v == "":
                v = None
            if k == "document_type":
                if "kind_id" not in effective:
                    effective["kind_id"] = v
                continue
            effective[k] = v
        for k, v in effective.items():
            setattr(doc, k, v)
        await db.flush()

        files = (
            (
                await db.execute(
                    select(DocumentFile)
                    .where(DocumentFile.document_id == doc.id)
                    .order_by(DocumentFile.sequence.asc())
                )
            )
            .scalars()
            .all()
        )

        commit: CommitResult = await record_versioned_change(
            db,
            patient=patient,
            user=user,
            request=request,
            entity_kind="patient_document",
            entity_id=doc.id,
            payload=_document_versioning_payload(doc, files),
            message=(f"[document] bulk edit ({', '.join(sorted(item.fields_set)) or 'no-op'})"),
            actor_override=actor_override,
        )
        head_etag = commit.commit_hash.hex()

        out_items.append(
            BulkItemResult(
                document_id=item.document_id,
                status="ok",
                diff=diff,
                etag=head_etag,
            )
        )
        n_ok += 1

    return BulkResult(
        items=out_items,
        n_ok=n_ok,
        n_error=n_error,
        n_dry_run=n_dry,
        head_etag=head_etag,
    )


__all__ = ["BulkItemResult", "BulkResult", "BulkUpdateItem", "apply_bulk_update"]
