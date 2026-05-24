"""Versioning schema: git-like history of every fascicolo.

The eight tables in this module replicate git's internal data model in
PostgreSQL: content-addressed payload blobs, commit DAG, mutable refs
that point at commit heads, plus pull-request scaffolding (proposals
and merge conflicts) and a sidecar table for binary blobs that live in
S3.

Design notes (see ``plans/zazzy-honking-nygaard.md`` for the full plan):

  * The "repository" is the patient. Every commit row carries a
    ``patient_id`` so all walks scope to one fascicolo.
  * Each entity (clinical_note, report, tag, ...) is identified by a
    stable ``entity_id`` (UUID) that survives across versions. Different
    versions carry different ``object_hash`` payload references.
  * ``object_hash`` and ``commit_hash`` are 32-byte BYTEA (raw sha256
    digests). BYTEA is half the size of hex TEXT and compares
    byte-for-byte without case-folding. The ``commit_hash`` value is
    the sha256 of the canonicalized commit header (parents + tree +
    author + message + timestamp).
  * Branches are named per-patient: ``main`` is always present;
    ``consultation/<consultation_id>`` is materialised when a
    consultation opens (see ``services/versioning.py``).
  * Storage efficiency: ``entity_objects`` carries a ``storage_kind``
    flag and optional delta encoding fields so a periodic ``pack``
    worker can replace long chains of full payloads with delta-encoded
    successors (mirrors git's loose-vs-pack lifecycle). Initial writes
    always store ``storage_kind='full'``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import BYTEA as PG_BYTEA
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Allowed entity_kind values inside ``entity_objects``. These map to
# tables in the rest of the schema (``clinical_notes``, ``reports``,
# ``annotations``, ...). The reserved ``_tree_`` kind stores the
# canonicalised manifest of a commit (see ``Commit.tree_hash``).
ENTITY_KINDS: tuple[str, ...] = (
    "patient",
    "study",
    "series",
    "report",
    "annotation",
    "tag",
    "clinical_note",
    "patient_document",
    "consultation",
    "summary",
    "measurement",
    "segmentation",
    "_tree_",  # internal: the manifest blob of a commit
)


class EntityObject(Base):
    """Immutable, content-addressed entity payload.

    Stored full by default (``storage_kind='full'``); a periodic pack
    worker can replace long chains with delta-encoded successors that
    reference a parent payload via ``delta_parent_hash`` and the bsdiff
    bytes in ``delta_bytes``. Reads via ``services/versioning.py``
    transparently walk the chain.
    """

    __tablename__ = "entity_objects"

    object_hash: Mapped[bytes] = mapped_column(PG_BYTEA, primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Full payload (canonicalised JSON). NULL when ``storage_kind='delta'``.
    payload: Mapped[dict | None] = mapped_column(JSONB)
    payload_size: Mapped[int] = mapped_column(Integer, nullable=False)

    # Pack/delta encoding lifecycle. See ``services/versioning.read_object``.
    # ``'full'`` keeps the canonical JSON in ``payload``; ``'delta'`` keeps a
    # bsdiff-against-parent in ``delta_bytes`` and walks back via
    # ``delta_parent_hash``; ``'s3'`` (F12.8) parks the canonical bytes on
    # S3 / MinIO under (``s3_bucket``, ``s3_key``) so cold history does
    # not pin Postgres storage. The CHECK invariant below enforces that
    # exactly one of the three column-sets is populated.
    storage_kind: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'full'")
    )
    delta_parent_hash: Mapped[bytes | None] = mapped_column(PG_BYTEA)
    delta_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)

    # F12.8 cold-tier indirection. Populated by the tier-down worker
    # (``services/versioning.tier_down_entity_objects``) when an old
    # ``'full'`` row is moved to S3. ``s3_etag`` is advisory; some
    # backends do not expose a stable etag for SSE / multipart uploads.
    s3_bucket: Mapped[str | None] = mapped_column(String(128))
    s3_key: Mapped[str | None] = mapped_column(Text)
    s3_etag: Mapped[str | None] = mapped_column(String(64))

    # GDPR tombstoning: keeps the row (so commit/manifest references stay
    # valid) but zeroes the payload. ``services/erasure.py`` is the only
    # caller that flips this.
    is_tombstoned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstoned_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "octet_length(object_hash) = 32",
            name="ck_entity_objects_hash_len",
        ),
        CheckConstraint(
            "entity_kind IN (" + ",".join(f"'{k}'" for k in ENTITY_KINDS) + ")",
            name="ck_entity_objects_kind",
        ),
        CheckConstraint(
            "storage_kind IN ('full','delta','s3')",
            name="ck_entity_objects_storage_kind",
        ),
        # Invariants between storage_kind and the related fields. Each
        # tier owns a disjoint column-set; mismatch is rejected at insert.
        CheckConstraint(
            "(storage_kind = 'full' AND payload IS NOT NULL "
            "AND delta_parent_hash IS NULL AND delta_bytes IS NULL "
            "AND s3_bucket IS NULL AND s3_key IS NULL) "
            "OR (storage_kind = 'delta' AND payload IS NULL "
            "AND delta_parent_hash IS NOT NULL AND delta_bytes IS NOT NULL "
            "AND s3_bucket IS NULL AND s3_key IS NULL) "
            "OR (storage_kind = 's3' AND payload IS NULL "
            "AND delta_parent_hash IS NULL AND delta_bytes IS NULL "
            "AND s3_bucket IS NOT NULL AND s3_key IS NOT NULL)",
            name="ck_entity_objects_storage_invariant",
        ),
        Index("ix_entity_objects_kind", "entity_kind"),
        Index("ix_entity_objects_created", "created_at"),
        Index(
            "ix_entity_objects_delta_parent",
            "delta_parent_hash",
            postgresql_where=text("delta_parent_hash IS NOT NULL"),
        ),
    )


class Commit(Base):
    """Anchor of the DAG; one row per save.

    Children commit point to parents via ``parent_hashes`` (zero for
    root, one for normal commits, two for merge commits). The pair
    ``(patient_id, commit_hash)`` is the primary access path; the
    GIN index on ``parent_hashes`` accelerates ancestor walks.
    """

    __tablename__ = "commits"

    commit_hash: Mapped[bytes] = mapped_column(PG_BYTEA, primary_key=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # sha256 of the canonical manifest. Equal-state commits share tree_hash
    # (useful for no-op merge detection).
    tree_hash: Mapped[bytes] = mapped_column(PG_BYTEA, nullable=False)

    # Parent DAG: 0 for root, 1 for regular, 2 for merge. Stored as
    # BYTEA[] so we can GIN-index ancestor queries.
    parent_hashes: Mapped[list[bytes]] = mapped_column(
        ARRAY(PG_BYTEA), nullable=False, server_default=text("'{}'::bytea[]")
    )

    # AI / human / system author fields. ``author_subject_id`` is NULL
    # only for system commits (initial-import migration, schema
    # evolution batches).
    author_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    author_kind: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'human'")
    )
    model_id: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(64))
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    # Direct FK to the AI assistant that authored the commit. The
    # legacy ``agent_token_id`` path stays in place for v2.1.x JWT
    # tokens that already pinned a row id, but the modern per-assistant
    # client_secret flow leaves ``agent_token_id`` NULL and only
    # populates this column. ``ON DELETE SET NULL`` because revoking
    # an assistant must not rewrite history. The revision-history join
    # in ``api/history.py`` reads through this column first and
    # falls back to the agent_tokens path for backward compat.
    agent_assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_assistants.id", ondelete="SET NULL"),
    )
    # Set when the commit was authored via an anonymous share link
    # (``share_links.mode='anonymous'``); the revision-history UI
    # uses it to render a "modality A" badge so reviewers can see at
    # a glance which writes came from a token-only credential rather
    # than a verified human session. ON DELETE SET NULL: revoking a
    # share link must not rewrite history.
    share_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("share_links.id", ondelete="SET NULL"),
    )

    # The branch name that was active when the commit was created
    # (debugging hint, NOT the source-of-truth for which branch carries it).
    branch_at_creation: Mapped[str | None] = mapped_column(String(128))

    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Postgres txid for forensic correlation with WAL.
    db_txid: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("txid_current()")
    )
    app_version: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "octet_length(commit_hash) = 32",
            name="ck_commits_hash_len",
        ),
        CheckConstraint("octet_length(tree_hash) = 32", name="ck_commits_tree_hash_len"),
        CheckConstraint(
            "cardinality(parent_hashes) BETWEEN 0 AND 2",
            name="ck_commits_parent_arity",
        ),
        CheckConstraint(
            "author_kind IN ('human','agent','system','link')",
            name="ck_commits_author_kind",
        ),
        Index(
            "ix_commits_patient_created",
            "patient_id",
            "created_at",
            postgresql_using="btree",
        ),
        Index("ix_commits_patient_tree", "patient_id", "tree_hash"),
        Index("ix_commits_author", "author_subject_id", "created_at"),
        Index(
            "ix_commits_parents_gin",
            "parent_hashes",
            postgresql_using="gin",
        ),
    )


class ManifestEntry(Base):
    """Exploded manifest: one row per (commit, entity_kind, entity_id).

    A commit's manifest tells us the object_hash of every clinical
    entity present at that snapshot. Storing it exploded (as opposed
    to packed in a single tree blob) lets normal SQL JOINs do the
    "state at commit C" query without parsing JSON. Storage cost is
    paid; deduplication still happens because unchanged entities point
    at the same object_hash across commits.
    """

    __tablename__ = "manifest_entries"

    commit_hash: Mapped[bytes] = mapped_column(
        PG_BYTEA,
        ForeignKey("commits.commit_hash", ondelete="CASCADE"),
        nullable=False,
    )
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    object_hash: Mapped[bytes] = mapped_column(
        PG_BYTEA,
        ForeignKey("entity_objects.object_hash", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "commit_hash",
            "entity_kind",
            "entity_id",
            name="pk_manifest_entries",
        ),
        Index("ix_manifest_object", "object_hash"),
        Index("ix_manifest_kind_entity", "entity_kind", "entity_id"),
    )


class Ref(Base):
    """Branch head: a (patient, ref_name) tuple pointing at a commit.

    Naming convention (enforced by code in services/versioning):
      * ``main``: persistent main branch of the patient
      * ``consultation/<consultation_id>``: branch tied to a
        consultation; lifecycle bound to consultations.status

    No other ref_name patterns are emitted by the system.
    """

    __tablename__ = "refs"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    ref_name: Mapped[str] = mapped_column(String(128), nullable=False)
    commit_hash: Mapped[bytes] = mapped_column(
        PG_BYTEA,
        ForeignKey("commits.commit_hash", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    visibility: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'private'")
    )
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("patient_id", "ref_name", name="pk_refs"),
        CheckConstraint(
            "visibility IN ('private','shared','public')",
            name="ck_refs_visibility",
        ),
        Index("ix_refs_owner", "owner_subject_id"),
        Index("ix_refs_commit", "commit_hash"),
    )


class RefLog(Base):
    """Append-only history of every ref movement (git reflog).

    Whenever ``services/versioning`` updates a ref, a row is inserted
    here with the previous and new commit_hash plus the operation kind
    ('init', 'commit', 'merge', 'reset', 'revert', 'rebase', 'delete').
    Used by audit and by potential undo flows.
    """

    __tablename__ = "ref_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    ref_name: Mapped[str] = mapped_column(String(128), nullable=False)
    from_commit: Mapped[bytes | None] = mapped_column(PG_BYTEA)
    to_commit: Mapped[bytes] = mapped_column(PG_BYTEA, nullable=False)
    op_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "op_kind IN ('init','commit','merge','reset','revert','rebase','delete')",
            name="ck_ref_log_op_kind",
        ),
        Index(
            "ix_reflog_patient_ref_time",
            "patient_id",
            "ref_name",
            "created_at",
        ),
    )


class Proposal(Base):
    """Pull-request between two refs of the same patient.

    Lifecycle is bound 1:1 to ``consultations.status`` (see plan
    ``L'esperienza utente``): the user only sees the consultation, the
    proposal is the technical record of what to merge where.
    """

    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    consultation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("consultations.id", ondelete="SET NULL"),
    )
    source_ref_name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_ref_name: Mapped[str] = mapped_column(String(128), nullable=False)

    source_head_commit: Mapped[bytes] = mapped_column(
        PG_BYTEA,
        ForeignKey("commits.commit_hash", ondelete="RESTRICT"),
        nullable=False,
    )
    target_head_commit: Mapped[bytes] = mapped_column(
        PG_BYTEA,
        ForeignKey("commits.commit_hash", ondelete="RESTRICT"),
        nullable=False,
    )
    base_commit: Mapped[bytes | None] = mapped_column(
        PG_BYTEA,
        ForeignKey("commits.commit_hash", ondelete="RESTRICT"),
    )

    proposer_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    merge_commit: Mapped[bytes | None] = mapped_column(
        PG_BYTEA,
        ForeignKey("commits.commit_hash", ondelete="RESTRICT"),
    )

    reviewed_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_decision: Mapped[str | None] = mapped_column(String(16))
    review_notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','approved','rejected','merged','withdrawn','superseded')",
            name="ck_proposals_status",
        ),
        CheckConstraint(
            "review_decision IS NULL OR review_decision IN ('approve','request_changes','reject')",
            name="ck_proposals_review_decision",
        ),
        Index("ix_proposals_patient_status", "patient_id", "status"),
        Index("ix_proposals_proposer", "proposer_subject_id", "status"),
        Index("ix_proposals_consultation", "consultation_id"),
    )


class MergeConflict(Base):
    """Cached conflict detection for a proposal review.

    Populated when the proposal is opened and re-populated after a
    rebase/refresh. The UI consumes these rows to render the review
    panel (one entry per entity in conflict). The user's resolution
    is recorded by setting ``resolution`` and ``resolved_object_hash``.
    """

    __tablename__ = "merge_conflicts"

    id: Mapped[uuid.UUID] = uuid_pk()
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    base_object_hash: Mapped[bytes | None] = mapped_column(
        PG_BYTEA,
        ForeignKey("entity_objects.object_hash", ondelete="RESTRICT"),
    )
    source_object_hash: Mapped[bytes | None] = mapped_column(
        PG_BYTEA,
        ForeignKey("entity_objects.object_hash", ondelete="RESTRICT"),
    )
    target_object_hash: Mapped[bytes | None] = mapped_column(
        PG_BYTEA,
        ForeignKey("entity_objects.object_hash", ondelete="RESTRICT"),
    )

    conflict_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    resolution: Mapped[str | None] = mapped_column(String(16))
    resolved_object_hash: Mapped[bytes | None] = mapped_column(
        PG_BYTEA,
        ForeignKey("entity_objects.object_hash", ondelete="RESTRICT"),
    )
    resolved_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "conflict_kind IN ('add_add','edit_edit','edit_delete','delete_edit')",
            name="ck_merge_conflicts_kind",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN "
            "('take_source','take_target','manual','auto_merge')",
            name="ck_merge_conflicts_resolution",
        ),
        UniqueConstraint(
            "proposal_id",
            "entity_kind",
            "entity_id",
            name="uq_merge_conflicts_proposal_entity",
        ),
    )


class BinaryBlob(Base):
    """Indirection record for content-addressed S3 binary payloads.

    The big stuff (NIfTI segmentation masks, multi-page PDFs, JPEG
    scans of paper reports) lives in S3 with a sha256-derived key. The
    JSON payload of an ``entity_objects`` row that owns one of these
    blobs carries only ``{content_hash, size_bytes, format}``; this
    table holds the bucket/key + GC refcount.
    """

    __tablename__ = "binary_blobs"

    content_hash: Mapped[bytes] = mapped_column(PG_BYTEA, primary_key=True)
    s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))

    is_tombstoned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refcount: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "octet_length(content_hash) = 32",
            name="ck_binary_blobs_hash_len",
        ),
        # Refcount can never go negative.
        CheckConstraint("refcount >= 0", name="ck_binary_blobs_refcount_nonneg"),
        # Partial index for GC scan: only blobs eligible for cleanup.
        Index(
            "ix_binary_blobs_refcount_zero",
            "refcount",
            postgresql_where=text("refcount = 0 AND is_tombstoned = false"),
        ),
    )
