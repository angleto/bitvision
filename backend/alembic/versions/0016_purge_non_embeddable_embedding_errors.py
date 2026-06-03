"""Purge non-actionable BiomedCLIP embedding_errors rows.

Until 0015, every DICOM series was enqueued for BiomedCLIP image embedding
regardless of modality. Non-image series — Structured Reports (SR),
Presentation States (PR), Segmentations (SEG), and the RT family — can never
carry a diagnostic image, so each one produced a permanent ``embedding_errors``
row (``NoPixelDataError`` for SR/PR, a decode ``TypeError`` for SEG) and a
retry storm. v4.3.4 fixes the root cause: the enqueue paths now filter by
modality (``bvphoenix.services.embeddable``) and the worker returns a terminal
``skipped`` status instead of raising on a non-image series.

This migration is the one-time data cleanup of the rows the old behaviour left
behind, so the admin coverage dashboard reflects reality (genuine failures
only). The predicate is narrow on purpose — it deletes a row only when:

* the target series' Modality is a known non-image one (SR/PR/SEG/RT/...), OR
* the error is a ``NoPixelDataError`` — which is *structurally* terminal (the
  DICOM object has no ``PixelData`` element; it is never retryable), so it is
  safe to drop regardless of modality (this also clears the no-pixel rows on
  the handful of MR series that carry no image).

It NEVER deletes by a generic error class alone: a transient ``S3Error`` /
``NetworkError`` / ``CudaOOM`` / ``TimeoutError`` on a real CT/MR/DX/CR/PT
series is preserved so the admin can still retry it.

Revision ID: 0016_purge_non_embeddable_embedding_errors
Revises: 0015_bge_m3_sparse_colbert
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

from bvphoenix.services.embeddable import NON_EMBEDDABLE_MODALITIES

revision = "0016_purge_non_embeddable_embedding_errors"
down_revision = "0015_bge_m3_sparse_colbert"
branch_labels = None
depends_on = None

# Inlined from the single source of truth (uppercase alphanumeric tokens —
# no injection surface). Keeping the import means the blocklist can never
# drift from the runtime filter.
_BLOCKED = ", ".join(f"'{m}'" for m in sorted(NON_EMBEDDABLE_MODALITIES))


def upgrade() -> None:
    op.execute(
        f"""
        DELETE FROM embedding_errors ee
        WHERE ee.model_id = 'biomedclip-v1'
          AND ee.target_kind = 'series'
          AND (
            EXISTS (
              SELECT 1 FROM series s
              WHERE s.id = ee.target_id
                AND s.modality IS NOT NULL
                AND UPPER(TRIM(s.modality)) IN ({_BLOCKED})
            )
            OR ee.error_class = 'NoPixelDataError'
          )
        """
    )


def downgrade() -> None:
    # No-op: deleted error rows cannot be resurrected, and they were
    # non-actionable noise. The worker would re-create them only if the
    # 0015-era unfiltered enqueue behaviour were also reverted.
    pass
