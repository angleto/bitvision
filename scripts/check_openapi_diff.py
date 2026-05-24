"""CI guard: regenerate the OpenAPI snapshot in memory and diff it
against the committed copy.

Exit codes:
* 0 — snapshot matches the live schema.
* 1 — drift detected. The diff is printed to stderr together with the
  remediation hint.

Pair with ``scripts/dump_openapi.py`` (the regeneration entry point)
and the ``openapi-check`` GitHub Actions workflow.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path


def _committed_path() -> Path:
    return Path(__file__).resolve().parent.parent / "backend" / "openapi.json"


def main() -> int:
    from bvphoenix.main import app  # noqa: WPS433

    schema = app.openapi()
    fresh = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    snapshot_path = _committed_path()
    if not snapshot_path.exists():
        print(
            f"ERROR: {snapshot_path} is missing. Run "
            "`uv run python scripts/dump_openapi.py` and commit the file.",
            file=sys.stderr,
        )
        return 1

    committed = snapshot_path.read_text(encoding="utf-8")
    if fresh == committed:
        return 0

    diff = "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            fromfile="committed openapi.json",
            tofile="live schema",
            n=3,
        )
    )
    print(diff, file=sys.stderr)
    print(
        "\nOpenAPI drift detected. Run "
        "`uv run python scripts/dump_openapi.py` and commit the updated "
        "snapshot in the same PR.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
