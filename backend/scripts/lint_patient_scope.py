"""Lint: every public service function in the care_phases stack must take
``patient_id`` as the first keyword-only argument.

Why
---
Cross-patient is impossible by construction (composite FK, nested REST
routes); this lint adds a third defence at the *service layer* by
forcing the calling convention. A service function that omits
``patient_id`` from its kw-only signature can still be called, but it
loses the ability to filter by patient and risks slipping a
cross-patient query into the codebase.

Scope
-----
Run against the care-phase service modules:

* ``backend/src/bvphoenix/services/care_phases.py``
* ``backend/src/bvphoenix/services/care_phase_classifier.py``

Public functions (those with a name not starting with ``_``) must
declare ``patient_id`` as a keyword-only argument; failing functions
are listed and the script exits non-zero. Helpers (``_foo``) are
exempt because they are called only from inside this layer.

Usage
-----
    cd backend && uv run python scripts/lint_patient_scope.py

Returns 0 on success, 1 on any violation. Wire it into CI to enforce
the convention.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


_TARGET_FILES = [
    "src/bvphoenix/services/care_phases.py",
    "src/bvphoenix/services/care_phase_classifier.py",
]

# Functions that legitimately do not take patient_id (pure helpers
# that operate on already-fetched data or on parser output). Keep
# this allowlist tiny and well-justified.
_ALLOWLIST: set[str] = {
    "compute_input_hash",  # operates on a Sequence[ClinicalEvent] of one patient
    "parse_classifier_output",  # parses LLM text only, no DB
}


def _violations_in(path: Path) -> list[str]:
    """Return human-readable violation lines for one source file."""
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            if node.name.startswith("_") or node.name in _ALLOWLIST:
                continue
            kw_only = [arg.arg for arg in node.args.kwonlyargs]
            if "patient_id" not in kw_only:
                bad.append(
                    f"{path}:{node.lineno}: {node.name}() does not take "
                    f"'patient_id' as kw-only; got kwonly={kw_only!r}"
                )
    return bad


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    bad: list[str] = []
    for relpath in _TARGET_FILES:
        path = repo_root / relpath
        if not path.exists():
            print(f"WARN: lint target not found: {path}", file=sys.stderr)
            continue
        bad.extend(_violations_in(path))
    if bad:
        print("Patient-scope lint failures:", file=sys.stderr)
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"Patient-scope lint: OK ({len(_TARGET_FILES)} files clean)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
