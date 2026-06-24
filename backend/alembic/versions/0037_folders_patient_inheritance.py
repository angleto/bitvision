"""Folders: DB-level patient_id inheritance (no orphan patient folders).

Invariant: a folder nested under another folder MUST carry the same
``patient_id`` as its parent. A folder with ``parent_folder_id`` pointing
into a patient's tree but ``patient_id = NULL`` is an *orphan*: it is
invisible to every patient-scoped query (``WHERE patient_id = :pid``),
which silently breaks the fascicolo tree export (the contained study /
document lands at the archive root instead of under its folder path) and
the FE tree navigation.

Two paths produced such orphans:
- ``POST /patients/{id}/tree/folder`` (``create_patient_folder``) scoped a
  folder via a ``'patient'`` marker FolderItem and left the ``patient_id``
  column NULL.
- ``_ensure_subfolder`` in the bulk-ingest worker tolerated
  ``patient_id=None`` under a non-null parent.

This migration enforces the invariant at the database, so the orphan
state is *inexpressible* regardless of which code path writes a folder
(mirrors the cross-patient defense-in-depth philosophy):

1. **Backfill** existing rows: propagate the nearest ancestor's
   ``patient_id`` down any NULL chain (heals orphans at any depth; leaves
   genuine personal-workspace folders, whose whole chain is NULL,
   untouched).
2. **BEFORE INSERT OR UPDATE trigger** replicating the service-layer
   semantics already in ``api/folders.py``: when ``parent_folder_id`` is
   set, inherit the parent's ``patient_id`` if NULL, and reject an
   explicit value that disagrees with the parent (cross-patient nesting).

Root folders (``parent_folder_id IS NULL``) are untouched here and remain
governed by the existing ``ck_folders_root_shape`` CHECK
(``patient_id NOT NULL`` for roots). ``studies.patient_id`` stays nullable
on purpose: a public / OpenData study legitimately has no patient until
one is built and associated.

Revision ID: 0037_folders_patient_inheritance
Revises: 0036_submissions
Create Date: 2026-06-24
"""

from __future__ import annotations

from alembic import op

revision = "0037_folders_patient_inheritance"
down_revision = "0036_submissions"
branch_labels = None
depends_on = None


_BACKFILL = """
WITH RECURSIVE tree AS (
    SELECT id, patient_id, parent_folder_id
    FROM public.folders
    WHERE parent_folder_id IS NULL
    UNION ALL
    SELECT f.id,
           COALESCE(f.patient_id, t.patient_id) AS patient_id,
           f.parent_folder_id
    FROM public.folders f
    JOIN tree t ON f.parent_folder_id = t.id
)
UPDATE public.folders f
SET patient_id = t.patient_id
FROM tree t
WHERE f.id = t.id
  AND t.patient_id IS NOT NULL
  AND f.patient_id IS DISTINCT FROM t.patient_id;
"""

_FN = """
CREATE OR REPLACE FUNCTION public.folders_inherit_patient_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
        parent_pid uuid;
    BEGIN
        IF NEW.parent_folder_id IS NOT NULL THEN
            SELECT patient_id INTO parent_pid
            FROM public.folders
            WHERE id = NEW.parent_folder_id;

            IF NEW.patient_id IS NULL THEN
                -- Inherit: a folder under a parent belongs to the parent's
                -- patient (or stays NULL for personal-workspace nesting).
                NEW.patient_id := parent_pid;
            ELSIF NEW.patient_id IS DISTINCT FROM parent_pid THEN
                -- Reject cross-patient (or patient-under-workspace) nesting.
                RAISE EXCEPTION
                    'folder_patient_mismatch: folder patient_id % must equal parent % patient_id %',
                    NEW.patient_id, NEW.parent_folder_id, parent_pid
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$;
"""

_TRIGGER = """
CREATE TRIGGER trg_folders_inherit_patient_id
    BEFORE INSERT OR UPDATE ON public.folders
    FOR EACH ROW
    EXECUTE FUNCTION public.folders_inherit_patient_id();
"""


def upgrade() -> None:
    # 1. Heal existing orphan folders (NULL patient_id under a patient parent).
    op.execute(_BACKFILL)
    # 2. Make the orphan state inexpressible going forward.
    op.execute(_FN)
    op.execute(_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_folders_inherit_patient_id ON public.folders;")
    op.execute("DROP FUNCTION IF EXISTS public.folders_inherit_patient_id();")
    # The backfill is a data correction; it is intentionally not reverted.
