"""Dump the FastAPI OpenAPI schema to ``backend/openapi.json``.

ADR 0015 — the snapshot must be deterministic so PR diffs surface every
endpoint or model change. We:

* call ``app.openapi()`` once,
* serialise with ``sort_keys=True`` and ``indent=2``,
* end the file with a trailing newline (POSIX-friendly).

Run this whenever you touch a FastAPI route or a Pydantic schema. CI
re-runs the same command and fails if the snapshot drifts (see
``scripts/check_openapi_diff.py``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _output_path() -> Path:
    """Resolve the snapshot path relative to the repo root.

    The script is committed at ``scripts/dump_openapi.py``; the
    snapshot lives at ``backend/openapi.json``. We anchor on the script
    location to make the output deterministic regardless of the
    invoking shell's cwd.
    """
    return Path(__file__).resolve().parent.parent / "backend" / "openapi.json"


def dump() -> Path:
    # Lazy import: ``bvphoenix.main`` ships a side effect (PHI redaction
    # install + startup checks) so we only pay it when called.
    from bvphoenix.main import app  # noqa: WPS433

    schema = app.openapi()
    out = _output_path()
    out.write_text(
        json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out


if __name__ == "__main__":
    written = dump()
    print(f"OpenAPI snapshot written to {written}", file=sys.stderr)
