"""Resumable upload sessions (upload_sessions + upload_session_files).

Phase 0 of the recoverable bulk-upload rework (DESIGN.md §11.6). Adds the two
durable tables that let a bulk upload be created BEFORE any bytes
(``awaiting_bytes``) and resumed from the last server-acked offset after a
disconnect, replacing the legacy single non-resumable POST.

No behavior change on its own: ``/api/upload/bulk`` is untouched; the session
endpoints + service land in Phase 1 and the FE cutover in Phase 2. Idempotent
(IF NOT EXISTS), safe to re-run.

Revision ID: 0017_upload_sessions
Revises: 0016_purge_non_embeddable_embedding_errors
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0017_upload_sessions"
down_revision = "0016_purge_non_embeddable_embedding_errors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_subject_id uuid NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            patient_id uuid,
            folder_id uuid,
            tier varchar(2) NOT NULL DEFAULT 't1',
            keep_iso_archive boolean NOT NULL DEFAULT true,
            wrap_iso_in_folder boolean NOT NULL DEFAULT true,
            extract_iso_contents boolean NOT NULL DEFAULT true,
            status varchar(16) NOT NULL DEFAULT 'awaiting_bytes',
            declared_total_bytes bigint NOT NULL DEFAULT 0,
            received_total_bytes bigint NOT NULL DEFAULT 0,
            job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
            idempotency_key varchar(128),
            scope_ids uuid[],
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            CONSTRAINT ck_upload_sessions_status CHECK (
                status IN ('awaiting_bytes','uploading','ready','committed','aborted','expired')
            ),
            CONSTRAINT ck_upload_sessions_tier CHECK (tier IN ('t1','t2','t3','t4')),
            CONSTRAINT ck_upload_sessions_bytes_nonneg CHECK (
                declared_total_bytes >= 0 AND received_total_bytes >= 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_upload_sessions_idem_active_uniq
        ON upload_sessions (idempotency_key)
        WHERE idempotency_key IS NOT NULL
          AND status IN ('awaiting_bytes','uploading','ready')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_upload_sessions_owner_active
        ON upload_sessions (owner_subject_id, status)
        WHERE status IN ('awaiting_bytes','uploading','ready')
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_upload_sessions_expires ON upload_sessions (expires_at)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_upload_sessions_scope_ids_gin
        ON upload_sessions USING gin (scope_ids)
        WHERE scope_ids IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_session_files (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
            file_index integer NOT NULL,
            filename text NOT NULL,
            relative_path text,
            declared_size bigint NOT NULL,
            declared_sha256 varchar(64),
            s3_key text NOT NULL,
            s3_upload_id text,
            parts jsonb NOT NULL DEFAULT '[]'::jsonb,
            received_offset bigint NOT NULL DEFAULT 0,
            status varchar(16) NOT NULL DEFAULT 'pending',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_upload_session_files_idx UNIQUE (session_id, file_index),
            CONSTRAINT ck_upload_session_files_status CHECK (
                status IN ('pending','uploading','staged')
            ),
            CONSTRAINT ck_upload_session_files_bytes_nonneg CHECK (
                declared_size >= 0 AND received_offset >= 0
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_upload_session_files_session "
        "ON upload_session_files (session_id)"
    )


def downgrade() -> None:
    # Drop the child table first (FK to upload_sessions). Indexes drop with
    # their tables.
    op.execute("DROP TABLE IF EXISTS upload_session_files")
    op.execute("DROP TABLE IF EXISTS upload_sessions")
