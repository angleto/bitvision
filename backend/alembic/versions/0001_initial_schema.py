"""Squashed initial schema — full HEAD state as of 0109_telegram_link_codes.

Replaces the 109-step incremental chain that grew through alpha.1..alpha.62.
The single production deployment was at HEAD when this squash happened
(2026-05-13), so the legacy chain no longer carried value and slowed fresh-DB
bootstrap plus historical review.

Schema and bootstrap seed payloads live next to this file under
``alembic/initial/``:

* ``schema.sql`` — ``pg_dump --schema-only`` of a fresh DB migrated to HEAD,
  including tables, indexes, constraints, enums, functions, triggers.
* ``seeds.sql`` — ``pg_dump --data-only --column-inserts`` of rows that
  bootstrap migrations populate on every fresh DB (platform owner subject,
  app_settings defaults, embedding_models, document_kinds /
  document_provenances / document_authorities catalog, llm_rate_cards).

Cutover on the existing production DB is **not** an upgrade since the
schema is already there. Operator runs once::

    UPDATE alembic_version SET version_num = '0001_initial_schema';

See ``alembic/initial/CUTOVER.md`` for the procedure.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-13
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


_INITIAL_DIR = Path(__file__).resolve().parent.parent / "initial"


def _split_sql_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL script on top-level semicolons.

    Respects single-quoted strings (``''`` escape), double-quoted identifiers,
    dollar-quoted bodies (``$tag$...$tag$`` including the empty tag ``$$``),
    line comments (``-- ...``) and block comments (``/* ... */``).
    """

    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_single = False
    in_double = False
    dollar_tag: str | None = None
    in_line_comment = False
    in_block_comment = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            buf.append(ch)
            buf.append(nxt)
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            buf.append(ch)
            buf.append(nxt)
            in_block_comment = True
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                tag = sql[i : j + 1]
                dollar_tag = tag
                buf.append(tag)
                i = j + 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _execute_sql_file(name: str) -> None:
    sql = (_INITIAL_DIR / name).read_text(encoding="utf-8")
    for stmt in _split_sql_statements(sql):
        op.execute(stmt)


def upgrade() -> None:
    _execute_sql_file("schema.sql")
    _execute_sql_file("seeds.sql")
    # pg_dump emits ``SELECT pg_catalog.set_config('search_path', '', false)``
    # to force fully-qualified names in the dump. That session setting bleeds
    # into the rest of the alembic transaction and makes the unqualified
    # ``INSERT INTO alembic_version`` that alembic runs at the end fail with
    # ``relation does not exist``.
    op.execute("SET search_path TO public")


def downgrade() -> None:
    op.execute("DROP SCHEMA public CASCADE")
    op.execute("CREATE SCHEMA public")
