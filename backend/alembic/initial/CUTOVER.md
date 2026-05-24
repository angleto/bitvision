# Squashed initial — production cutover

## Context

On 2026-05-13 the 109-step incremental migration chain was collapsed into a
single revision `0001_initial_schema`. The production DB was already at HEAD
(`0109_telegram_link_codes`) at the time, so the cutover is purely a rewrite
of the `alembic_version` pointer — no DDL runs on production.

Source of truth for the squash:

* `schema.sql` — `pg_dump --schema-only --no-owner --no-privileges --no-comments`
  of a fresh DB migrated through the old chain to head, with the
  `alembic_version` table stripped (alembic manages it itself), and
  `\restrict`/`\unrestrict` psql metacommands stripped.
* `seeds.sql` — `pg_dump --data-only --column-inserts` of the seed tables
  populated by the legacy bootstrap migrations: `subjects` (platform owner),
  `app_settings`, `embedding_models`, `document_kinds`,
  `document_provenances`, `document_authorities`, `llm_rate_cards`.

One-shot backfills tied to legacy production data (e.g. `0079_backfill_required_consents`,
`0084_backfill_imaging_clinical_events`, `0085_clinical_events_source_and_doc_backfill`)
are **not** included: they operated on rows that already exist in prod and have
no analogue on a fresh DB.

## Cutover on production

Run **once** against the production DB, **after** deploying the image that
contains this squash:

```sql
UPDATE alembic_version SET version_num = '0001_initial_schema';
```

Verify:

```sql
SELECT version_num FROM alembic_version;  -- expect: 0001_initial_schema
```

```bash
uv run alembic current   # expect: 0001_initial_schema (head)
uv run alembic upgrade head   # expect: no-op
```

That is the entire cutover. The schema bytes on disk are unchanged.

## Fresh-DB bootstrap

For any new environment (dev laptop, staging, CI), the flow is the standard
one:

```bash
uv run alembic upgrade head
```

This will execute `schema.sql` then `seeds.sql` inside a single transaction,
landing the DB directly at `0001_initial_schema`.

## Cosmetic schema delta vs the legacy chain

A diff of `pg_dump --schema-only` between (a) a fresh DB that ran the old
109-step chain and (b) a fresh DB that ran only the squash reveals ~270 lines
of differences, **all** of the same shape:

```diff
-CONSTRAINT ck_x CHECK ((c)::text = ANY ((ARRAY['a','b'])::text[]))
+CONSTRAINT ck_x CHECK ((c)::text = ANY (ARRAY[('a')::text, ('b')::text]))
```

```diff
-CREATE INDEX ix_y ... WHERE (s = ANY ((ARRAY[...])::text[]));
+CREATE INDEX ix_y ... WHERE (s = ANY (ARRAY[(...)::text, ...]));
```

Both forms are **semantically identical** — the Postgres planner reduces them
to the same `ScalarArrayOpExpr`. The difference comes from how Postgres
canonicalizes a CHECK/predicate expression based on the exact SQL form it was
parsed from. The legacy chain originally went through
`op.create_check_constraint(text=...)` calls; the squash goes through a
re-dump of the resulting catalog state. Re-parse of the dumped form lands on
a slightly different canonicalization.

Implications:

* Production keeps the *legacy* canonical form on disk after the cutover.
  This is fine — the constraints still evaluate identically.
* Fresh dev DBs will have the *squash* canonical form. Also fine.
* `alembic --autogenerate` does not inspect CHECK constraint text, so no
  spurious diffs will appear.

## Rollback

If the cutover misbehaves and we need to walk back to the legacy state:

1. `git revert` the squash commit — this restores all 109 migration files.
2. On production, restore the version pointer:
   ```sql
   UPDATE alembic_version SET version_num = '0109_telegram_link_codes';
   ```
3. Future migrations resume from the legacy chain.

No schema change is required either direction — only the pointer moves.
