"""Embed workers admit the coarse text target kinds we now write.

Flow task 84220e21. The on-write coarse embed fans out over every active
text model, so both worker stores must admit ``report_content`` (the new
coarse arm) and ``finding`` (previously MiniLM-only). The matching DB CHECK
constraints are widened by migration 0026; these assert the worker-side
allow-lists agree, so an enqueue is not silently rejected as
``invalid_target_kind`` before it reaches the store.
"""

from __future__ import annotations

from bvworkers.tasks.embed_bge_m3 import ALLOWED_TARGET_KINDS as BGE_ALLOWED
from bvworkers.tasks.embed_text_multilingual import ALLOWED_TARGET_KINDS as ML_ALLOWED


def test_minilm_admits_coarse_targets():
    assert "report_content" in ML_ALLOWED
    assert "finding" in ML_ALLOWED  # added in migration 0021
    assert "document" in ML_ALLOWED
    assert "patient" in ML_ALLOWED


def test_bge_m3_admits_coarse_targets_including_finding():
    # 0021 left BGE-M3 untouched; this task widens it so findings + report
    # contents land in the BGE store once it is the active text model.
    assert "report_content" in BGE_ALLOWED
    assert "finding" in BGE_ALLOWED
    assert "document" in BGE_ALLOWED
    assert "patient" in BGE_ALLOWED
