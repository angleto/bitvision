"""Cross-patient guard on ReportContent.{narrative,findings,recommendations}_md.

POST /report-contents and PATCH /report-contents/{id} accept markdown
fields that may carry the same ``@kind:UUID`` mention DSL used by
clinical_notes and clinical_events. Without a server-side guard a
human or agent could persist a mention pointing at another patient's
resource, which violates the "cross-patient impossible by
construction" invariant (see memory ``cross_patient_links_forbidden``).

This file pins:

1. Structural: ``api/report_contents.py`` imports + invokes
   ``validate_mentions_or_raise`` from at least the create + patch +
   supersede write handlers. Catches accidental removal during
   refactor without needing a live HTTP stack.
"""

from __future__ import annotations

import inspect

from bvphoenix.api import report_contents as rc_api


def test_report_contents_module_imports_validator() -> None:
    """``validate_mentions_or_raise`` must be referenced by the module
    so a future refactor that removes the import is caught here."""
    src = inspect.getsource(rc_api)
    assert "validate_mentions_or_raise" in src, (
        "report_contents.py must call validate_mentions_or_raise on markdown "
        "writes; without it the cross-patient guard is bypassed for reports"
    )


def test_validator_called_in_all_write_paths() -> None:
    """Belt-and-braces: the validator must be invoked from every write
    handler that accepts markdown (``create_report_content``,
    ``update_report_content``, ``supersede_report_content``). We count
    call sites (``validate_mentions_or_raise(``, with the open paren)
    so the bare import on its own line is not counted."""
    src = inspect.getsource(rc_api)
    occurrences = src.count("validate_mentions_or_raise(")
    # 1 in create + 1 in patch + 1 in supersede = 3 call sites.
    assert occurrences >= 3, (
        f"expected ≥3 call sites of validate_mentions_or_raise (create + patch + supersede); "
        f"found {occurrences}"
    )
