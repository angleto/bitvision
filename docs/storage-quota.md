# Storage quota

F11.3 implements the **10 GiB free tier** (DESIGN.md §9). The cap
applies to **T1 (private) + T2 (shared controlled)** studies; T3
(anonymized opt-in) and T4 (public CC) are not counted against it.

## What counts

Only the **DICOM instance payload** counts. Specifically, the quota is

```sql
SELECT COALESCE(SUM(instances.size_bytes), 0)
FROM instances
JOIN series  ON series.id = instances.series_id
JOIN studies ON studies.id = series.study_id
WHERE studies.owner_subject_id = :user_subject_id
  AND studies.contribution_tier IN ('t1', 't2');
```

Deliberately excluded:
- **Derivatives** (thumbnails, MPR cache, packed NIfTI, tile pyramids).
  They are generated artefacts; charging the user for the platform's
  own caching choices would be unfair and would couple quota to
  derivative policy churn.
- **Patient documents** (PDFs, text, notes). They have no
  `size_bytes` column today and in practice are sub-MB; when the
  product ever allows >10 MB per document this needs revisiting.

## Where the check runs

`services/quota.py` exposes:

- `STORAGE_FREE_TIER_BYTES = 10 * 1024**3` (10 GiB binary).
- `get_user_storage_usage(db, user_subject_id) -> StorageUsage`.
- `check_quota_or_raise(db, *, user_subject_id, tier, incoming_bytes)`
  — enforces the cap before the upload commits. Raises
  `HTTPException(413)` with a structured detail when the projected
  total would exceed the cap. No-op for T3/T4.

Call sites:

- `api/dicom_upload.upload_studies` — `incoming = sum(len(blob))` over
  the drag-drop batch.
- `api/dicom_upload.stow_rs` — `incoming = len(body)` (MIME framing
  overhead rounds in the user's favour).
- `api/bulk_upload.bulk_upload` — `incoming = sum(len(vf.data))` over
  the staged, post-zip-unpack virtual-file set.

## Inspecting usage

`GET /api/me/storage` (authenticated) returns:

```json
{
  "used_bytes": 1234567890,
  "quota_bytes": 10737418240,
  "remaining_bytes": 9502850350,
  "tiers_counted": ["t1", "t2"]
}
```

Intended consumer: a settings page and a pre-upload confirmation
dialog that shows "You have X remaining before this upload".

## Best-effort semantics

The check is **pre-commit** and **best-effort**. A race where two
concurrent uploads each pass the check and together cross the cap is
possible; the overshoot is bounded by each request's individual size
limit (`MAX_STOW_BYTES` in `dicom_upload.py`, `MAX_FILE_BYTES` in
`bulk_upload.py`) and the next upload is rejected. We accept this
because:

1. The free tier is *free*. Overshoot is not revenue loss, only
   storage-cost skew.
2. A strict guarantee would require a SERIALIZABLE transaction that
   locks the user's study set, or a trigger-maintained `user_usage`
   summary table — both add moving parts that today's QPS does not
   justify.

When the commerce model evolves (paid tiers with hard caps, storage
billing), the enforcement moves into a ledger-style service with
idempotency keys and a reservation protocol — same pattern the F7
credit gateway will use.

## Future work

- Include `patient_documents` once that table grows a `size_bytes`
  column and files routinely cross the MB mark.
- Expose `GET /api/me/storage` on the settings page; today it is API
  only.
- Paid tier support: a second quota class with a hard ceiling and
  integration with the F7 credit ledger for overage billing.
