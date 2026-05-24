# crawler — public DICOM archive ingester

Admin-only CLI that populates the platform's public demo library from
curated open-license archives: TCIA (The Cancer Imaging Archive),
OpenNeuro, Radiopaedia, Cancer Imaging Archive, etc.

**Not exposed to end users.** Run manually or via scheduled jobs by
platform administrators.

## Stack

- Typer + Rich (CLI)
- `httpx` for HTTP
- `pydicom` for DICOM header validation
- `boto3` for S3 upload
- Plugin connectors under `src/bvcrawler/connectors/`

## Usage

```sh
make crawler.install
uv run bvcrawler version
uv run bvcrawler list-sources
uv run bvcrawler run --source tcia --collection LIDC --dry-run
```

## Adding a source

1. Create `src/bvcrawler/connectors/<source>.py` implementing the
   `Connector` protocol from `connectors/base.py`
2. Export a `CONNECTOR` instance
3. Register it in `connectors/__init__.py` under `CONNECTORS`

## Design rules (recap from `../docs/DESIGN.md` §5.5):

- Every crawled study defaults to **T4 (public CC)** and never to T3
  without explicit, documented consent from the source.
- Provenance is mandatory: `source_url`, `license`, `attribution`,
  `crawled_at`, `crawler_version`.
- Respect `robots.txt` and per-source rate limits / ToS.
- Reuse the standard ingestion pipeline — the crawler writes to S3
  and enqueues the normal ingestion job; it does not bypass parsing,
  de-identification, or embedding.

## What's here

TCIA NBIA v1 connector landed and was used to seed the OpenData
library on 2026-05-20 (LIDC-IDRI, QIN-BREAST, MIDRC-RICORD-1C; 35
patients / 154 studies / 5 082 instances). OpenNeuro and Radiopaedia
connectors are still TBD.

Field reports / quirks worth knowing before the next run:

- TCIA NBIA v4 API is dead; use v1.
- NBIA collection / study identifiers are case-sensitive.
- `QIN-HEADNECK` does not exist as a collection (collection name was a
  typo in early seed scripts).
- OsiriX is SSO-gated and not reachable from a headless run.
- External study ids land in `imaging_studies.external_identifiers`
  (JSONB), not in a legacy `external_id` column.
