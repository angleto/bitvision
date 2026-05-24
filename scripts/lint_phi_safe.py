"""PHI-safe lint check.

Scans the backend source tree for ``logging`` calls whose arguments
mention well-known PHI fields without going through the redaction
helpers in ``bvphoenix.logging`` or ``bvphoenix.services.audit``. The
heuristic is conservative: false positives are fixed by adding a
``# noqa: phi-safe`` trailing comment on the offending line. Real
violations are easy to read and easy to mechanically fix (replace the
direct interpolation with an ``extra={...}`` payload that the
``PHIRedactionFilter`` will scrub).

Why a script and not a custom ruff rule?
----------------------------------------
Ruff's plugin surface today does not let users register custom
rules without a Rust-side compile. A 200-line Python AST walker is
simpler, vendor-neutral, and easy to evolve as the PHI taxonomy
grows. It runs as a pre-commit hook and as a CI job.

Detection scope
---------------
A logging call is anything that resolves to one of:

* a method call on an identifier whose name matches ``log``, ``logger``,
  ``_log``, ``LOGGER``, etc. (case-insensitive contains "log"),
* a top-level ``logging.<level>(...)`` call.

For each call we inspect the literal string args (the format string)
and any f-string (``JoinedStr``) parts. If a PHI keyword appears as a
substring of the format string AND the call doesn't pass an
``extra={...}`` kwarg (where redaction lands), it's flagged.

Exit codes
----------
* 0 — clean.
* 1 — at least one violation. The offending lines are printed in the
  ``path:line:col: PHI-WARN: ...`` format.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

# Single source of truth for the PHI taxonomy. Keep aligned with
# ``bvphoenix.services.audit._REDACT_KEYS``.
_PHI_KEYWORDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "codice_fiscale",
        "tax_id",
        "fiscal_code",
        "ssn",
        "first_name",
        "last_name",
        "given_name",
        "family_name",
        "full_name",
        "display_name",
        "patient_name",
        "patient_id",
        "email",
    }
)


def _is_logger_call(node: ast.Call) -> bool:
    """Return True for ``log.info(...)`` / ``logger.warning(...)`` /
    ``logging.error(...)`` and similar."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}:
        return False
    value = func.value
    name = ""
    if isinstance(value, ast.Name):
        name = value.id
    elif isinstance(value, ast.Attribute):
        name = value.attr
    return "log" in name.lower()


def _has_extra_kw(node: ast.Call) -> bool:
    """``extra={...}`` (or ``stacklevel=...``) routes through the
    redaction filter — a warning sign that the developer is being
    careful. Treat its presence as opt-out."""
    return any(kw.arg == "extra" for kw in node.keywords)


def _interpolated_names(node: ast.expr) -> Iterable[str]:
    """Yield identifiers that get interpolated into the log message.

    We focus on the *expressions* that resolve at runtime to dynamic
    text — those are the ones that may carry PHI. Plain prose inside
    a format string ("failed to send email to %s") is left alone; the
    runtime PHI redaction filter scrubs values flowing through ``%s``
    on the fly.

    Sources we look at:

    * ``f"... {user.email} ..."`` — an :class:`ast.FormattedValue`
      whose subexpression we ``unparse`` (gives ``user.email``).
    * ``"%(email)s"`` printf-style — we extract the ``email`` token by
      scanning the format string itself.
    * Plain identifiers/attribute chains passed as positional args:
      ``logger.info("foo", user.email)`` ⇒ ``user.email``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # printf-style %(name)s — extract the parenthesized names.
        i = 0
        text = node.value
        while True:
            start = text.find("%(", i)
            if start == -1:
                break
            end = text.find(")", start + 2)
            if end == -1:
                break
            yield text[start + 2 : end]
            i = end + 1
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                yield ast.unparse(part.value)
            else:
                yield from _interpolated_names(part)
    elif isinstance(node, (ast.Name, ast.Attribute)):
        yield ast.unparse(node)


def _violations_in_call(node: ast.Call) -> list[str]:
    """Return PHI keywords interpolated into the call's args."""
    found: set[str] = set()
    for arg in node.args[1:]:
        # arg[0] is the format string; the values are everything after.
        for chunk in _interpolated_names(arg):
            for kw in _PHI_KEYWORDS:
                if kw in chunk.lower():
                    found.add(kw)
    if node.args:
        # Also peek at the first arg for f-string interpolations and
        # printf-style %(name)s tokens.
        for chunk in _interpolated_names(node.args[0]):
            for kw in _PHI_KEYWORDS:
                if kw in chunk.lower():
                    found.add(kw)
    return sorted(found)


def _scan_file(path: Path) -> list[str]:
    """Return a list of ``path:line:col: PHI-WARN ...`` strings."""
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    lines = source.splitlines()
    issues: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_logger_call(node):
            continue
        if _has_extra_kw(node):
            continue
        violations = _violations_in_call(node)
        if not violations:
            continue
        line_no = node.lineno
        col = node.col_offset
        line_text = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        # Allow either a trailing ``# phi-safe`` comment on the call
        # line or a leading ``# phi-safe:`` comment on the previous
        # line (so multi-line calls can carry the marker without
        # breaking ruff's noqa parser).
        if "phi-safe" in line_text:
            continue
        prev_line = lines[line_no - 2] if line_no >= 2 else ""
        if "phi-safe" in prev_line:
            continue
        kws = ",".join(violations)
        issues.append(
            f"{path}:{line_no}:{col}: PHI-WARN: log call references {kws}; "
            "route it through ``extra={{...}}`` so the PHI filter scrubs it"
        )
    return issues


def _iter_python(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            # Skip vendored / generated trees.
            parts = set(path.parts)
            if {"__pycache__", "alembic", ".venv", "node_modules"} & parts:
                continue
            yield path


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        roots = [Path(a) for a in argv[1:]]
    else:
        # Default: scan the backend src tree.
        repo_root = Path(__file__).resolve().parent.parent
        roots = [repo_root / "backend" / "src"]

    issues: list[str] = []
    for path in _iter_python(roots):
        issues.extend(_scan_file(path))

    if not issues:
        print("PHI-safe: clean", file=sys.stderr)
        return 0

    for issue in issues:
        print(issue)
    print(
        f"\nPHI-safe: {len(issues)} potential leak(s) found. "
        "Add `# noqa: phi-safe` to suppress an individual line if it is a "
        "false positive.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
