"""CI regression: every mutating route under ``/patients/{patient_id}/...``
in ``backend/src/bvphoenix/api/patients.py`` must call
``enforce_agent_patient_scope`` so a leaked agent token bound to
patient A cannot reach into patient B via that route.

Memory ``feedback_systemic_agent_patient_scope_gap`` records the
2026-05-x incident where 5 sibling modules had been audited but
``patients.py`` was left outstanding. This test pins the coverage so
the gap cannot reopen without a CI failure.

Implementation: parse the file with ``ast``, walk every async route
function decorated with ``@router.<method>("/patients/{patient_id}...")``
or ``@router_writes.<method>(...)`` whose method is one of POST /
PUT / PATCH / DELETE, and assert each one contains a
``enforce_agent_patient_scope(`` call somewhere in its body.

Helpers like ``_get_patient_or_404`` and ``_resolve_patient_for_write``
satisfy the requirement transparently because they call the guard
internally — we walk those bodies too via a small allowlist of
"trusted helper names" so the test doesn't require the audit author
to inline-call the guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
# patients.py was split into a package on 3.8.0; every child carries
# its own slice of the 42 endpoints. The audit walks every .py file
# in the package (including __init__.py and _shared.py, which is
# harmless: they don't define route handlers).
PATIENTS_PKG = BACKEND_ROOT / "src" / "bvphoenix" / "api" / "patients"

# Calls to these helpers count as "guard satisfied" because the helper
# itself invokes ``enforce_agent_patient_scope`` against the patient
# the handler is about to mutate. Keep the allow-list narrow.
TRUSTED_GUARDS = {
    "enforce_agent_patient_scope",
    "_get_patient_or_404",
    "_resolve_patient_for_write",
}

WRITE_METHODS = {"post", "put", "patch", "delete"}


def _decorator_method_and_path(dec: ast.expr) -> tuple[str, str] | None:
    """Return (method, path) for a ``@router.post("/patients/{patient_id}/x")``
    style decorator, or None if the decorator does not fit that shape."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name):
        return None
    if func.value.id not in {"router", "router_writes"}:
        return None
    method = func.attr.lower()
    if method not in WRITE_METHODS:
        return None
    if not dec.args:
        return None
    first = dec.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    return method, first.value


def _function_calls_a_trusted_guard(func: ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name) and target.id in TRUSTED_GUARDS:
                return True
            if isinstance(target, ast.Attribute) and target.attr in TRUSTED_GUARDS:
                return True
    return False


def test_every_patient_id_write_route_calls_a_trusted_guard() -> None:
    # Walk every child .py file under the api/patients/ package. Each
    # one defines its own ``router`` and a slice of the 42 routes;
    # collectively they cover what monolithic patients.py used to.
    missing: list[tuple[str, str, str, int]] = []
    seen = 0

    for py_file in sorted(PATIENTS_PKG.glob("*.py")):
        if py_file.name in {"__init__.py", "_shared.py"}:
            # _shared has no @router decorators; __init__.py only
            # aggregates child routers via include_router. Skipping
            # is safe.
            continue
        src = py_file.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                parsed = _decorator_method_and_path(dec)
                if parsed is None:
                    continue
                method, path = parsed
                if "{patient_id}" not in path:
                    continue
                seen += 1
                if not _function_calls_a_trusted_guard(node):
                    missing.append((py_file.name, method.upper(), path, node.lineno))
                # One write route per handler — break inner loop.
                break

    assert seen > 5, (
        f"sanity check: expected to find more than 5 patient-id "
        f"write routes, found {seen}. Did the file move?"
    )
    assert not missing, (
        "patients.py has write routes without a cross-patient guard "
        "(neither enforce_agent_patient_scope nor one of "
        f"{TRUSTED_GUARDS}): {missing}"
    )
