# Owner-scoped DICOM UIDs

DICOM identifiers are *supposed* to be globally unique
(StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID, plus the
PatientID tag) but in practice they aren't:

- vendors and PACS reuse them across sites;
- anonymisers often emit deterministic UIDs that collide;
- template scanner protocols inject the same UID into every study
  produced by the same machine;
- redacted public datasets share UIDs by design.

If we treated them as globally unique (the original schema enforced a
DB-level `UNIQUE` on each column), then user B's study would either
fail with a unique-violation when uploaded or — worse — graft data
onto user A's existing rows.

The fix lands in `0043_scope_dicom_uids_to_owner` and the matching
ORM models. Within one user's namespace the UIDs are still treated as
unique; **across users they may freely collide**.

## DB schema

| Table | New constraint | Replaces |
|---|---|---|
| `studies`   | `UNIQUE(owner_subject_id, study_instance_uid)`   | `studies_study_instance_uid_key` |
| `series`    | `UNIQUE(study_id, series_instance_uid)`          | `series_series_instance_uid_key` |
| `instances` | `UNIQUE(series_id, sop_instance_uid)`            | `instances_sop_instance_uid_key` |
| `patients`  | partial `UNIQUE(managed_by_subject_id, external_id) WHERE external_id IS NOT NULL` | nothing — `external_id` was indexed but not unique |

Secondary non-unique indexes survive on the bare UID columns so admin
/ cross-owner search stays fast.

## Ingest path

`services/dicom_ingest.py` now scopes every lookup to the owner and
to the parent row:

```python
# look up an existing study by *(owner, UID)* — never by UID alone
row = await db.execute(
    select(Study).where(
        Study.study_instance_uid == study_uid,
        Study.owner_subject_id == self._owner.id,
    )
)
```

The same pattern applies to `_get_or_create_series` (filters by
`study_id`) and to the SOP-UID dedup inside `ingest_blob` (filters by
`series_id`). `cli/import_dicom.py` mirrors the pattern for the
synchronous bulk-import path.

## S3 keys

S3 paths used to be `studies/<study_uid>/series/<series_uid>/<sop_uid>.dcm`.
That shape would silently overwrite blobs across users when UIDs
collided. The key is now namespaced by the owner:

```
users/<owner_subject_id>/studies/<study_uid>/series/<series_uid>/<sop_uid>.dcm
```

Both `services.dicom_ingest.s3_key_for` and the CLI's `_s3_key`
produce the same shape.

## Migration / ops note

Migration `0043_scope_dicom_uids_to_owner` is destructive on the
constraint level (it drops the old global UNIQUEs and adds composite
ones) but does not move existing rows. Pre-migration rows continue to
work because their `(owner, UID)` pairs are already de-facto unique
within the existing data. Existing S3 blobs sitting under the legacy
`studies/...` prefix are not relocated — they remain reachable via
the `Instance.s3_key` column, which preserves the literal path.
Future uploads land under the new prefix.
