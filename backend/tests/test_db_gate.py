"""Guard: the DB-backed gate must never go green by skipping.

Every DB-touching file in this suite is marked ``skip_if_no_db``, and
that mark is decided by ``_have_db()`` in ``conftest.py`` -- a 200 ms TCP
probe that returns ``False`` silently. On CI that is a real hazard: the
``backend-db-test`` job stands up a Postgres service and is the stated
gate for the clinical-event date fix, but if the probe misses (a slow
runner, IPv6-first resolution of ``localhost``, a remapped port) pytest
would report every one of those tests as skipped and the job would pass
having executed none of them.

So CI arms ``BVP_REQUIRE_DB=1``. With the flag set, ``skip_if_no_db``
stops skipping (the tests run and fail on the connection if the DB is
genuinely absent) and this test states the diagnosis in one line instead
of leaving it to be inferred from a wall of ``ConnectionRefusedError``.
Local runs, which do not set the flag, keep skipping exactly as before.

Same shape as ``test_ocr_gate_not_silently_skipped`` in
``test_pixel_deid_redaction.py``, which arms ``BVP_REQUIRE_OCR`` for the
pixel-PHI recall gate.
"""

from __future__ import annotations

import os

import pytest

from .conftest import _HAVE_DB


def test_db_gate_not_silently_skipped() -> None:
    if os.environ.get("BVP_REQUIRE_DB") != "1":
        pytest.skip("BVP_REQUIRE_DB unset (local run)")
    assert _HAVE_DB, (
        "BVP_REQUIRE_DB=1 but no Postgres answered on the configured "
        "BVP_DATABASE_URL host:port - every skip_if_no_db test would have "
        "skipped and this job would have reported green without running the "
        "DB-backed gate"
    )
