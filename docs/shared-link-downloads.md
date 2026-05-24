# Shared-link downloads — reports and patient documents

Extension of [sharing.md](./sharing.md) that lets the holder of a valid
share link pull down the non-DICOM artifacts attached to the shared
resource: radiology **reports** (PDF/DOCX files saved on the `Report`
row) and, for patient-scoped links, the **patient documents** that make
up the fascicolo (consents, discharge letters, lab results, etc.).

Before this extension the shared landing page could show metadata but
not hand the recipient the actual PDF — a blocker for the most common
consultation flow, where the consulting radiologist wants the original
report alongside the pixels.

---

## 1. Permission model

Adds a single permission alias in `bvphoenix.services.permissions`:

```python
SHARED_DOWNLOAD = "download:derivative"
```

It intentionally aliases the existing `download:derivative` verb rather
than minting a new one. That verb is already produced by
`level_to_permissions(..., download=True)` — the owner opts into
downloads by checking the **Download** box in the share dialog, same as
for DICOM, and the bundle automatically covers derivatives (reports,
patient documents).

The share-link download endpoints are the **only** surface that checks
`SHARED_DOWNLOAD` today; the authenticated study API (`/api/studies/...`)
keeps using `DOWNLOAD_DICOM` / `DOWNLOAD_DERIVATIVE` directly.

---

## 2. Endpoints

All three live in `backend/src/bvphoenix/api/sharing.py`.

### 2.1. `GET /api/shared/{token}/artifacts`

Lists the downloadable files exposed by a valid share link. Passive —
does not bump `use_count`.

```jsonc
{
  "can_download": true,           // SHARED_DOWNLOAD in grant.permissions
  "reports": [
    {
      "id": "uuid",
      "study_id": "uuid",
      "version": 2,
      "title": "first 80 chars of report text",
      "file_content_type": "application/pdf",
      "created_at": "2026-04-17T10:00:00Z"
    }
  ],
  "documents": [                  // patient grants only, else []
    {
      "id": "uuid",
      "document_type": "discharge_letter",
      "title": "Dimissioni 2026-03-15",
      "file_content_type": "application/pdf",
      "document_date": "2026-03-15",
      "created_at": "2026-04-17T10:00:00Z"
    }
  ]
}
```

For study-scoped links, `documents` is always empty — patient documents
are not considered study artifacts and are never exposed through a
study share. The frontend uses `can_download` to decide whether to
render the Downloads card at all.

### 2.2. `GET /api/shared/{token}/reports/{report_id}/download`

Returns `307 Temporary Redirect` to a presigned S3 GET URL (1h TTL,
capped by `jwt_expires_seconds`). Mirrors the pattern in
`download_instance` (`api/studies.py`).

The endpoint validates, in order:

1. The share link exists (`SELECT ... FOR UPDATE` on `share_links`).
2. The grant is not revoked, not expired, and under `max_uses`.
3. If the link is password-protected, the request carries a valid
   session JWT in `Authorization: Bearer` — the same JWT issued by
   `POST /api/shared/{token}/verify`.
4. `SHARED_DOWNLOAD` is in `grant.permissions`.
5. The report belongs to the shared resource:
   - **study grant** → `report.study_id == grant.resource_id`
   - **patient grant** → `report.study_id` resolves to a study whose
     `patient_id == grant.resource_id`

On success the request atomically bumps `use_count += 1` inside the
transaction holding the row lock, commits, then redirects.

### 2.3. `GET /api/shared/{token}/documents/{doc_id}/download`

Identical guards plus: `grant.resource_kind == "patient"` (patient
documents only ride patient grants) and `PatientDocument.patient_id ==
grant.resource_id`.

---

## 3. Session / password posture

The share link itself is the primary capability. For passwordless links
we require nothing beyond holding the URL — anyone with the link could
already fetch `/info` and see the metadata, and the grant is what
ultimately decides what they can do.

For **password-protected** links the caller must first hit
`POST /api/shared/{token}/verify` and then forward the returned
short-lived JWT on every download. `_require_session_token` in
`sharing.py` decodes the JWT and asserts `sub == PUBLIC_SUBJECT_ID`.
This ties the download session to a successful password check without
any server-side session store.

Revocation is real-time: because every download re-reads the `Grant`
row, setting `revoked_at` stops new downloads immediately, even for
session JWTs that have not yet expired.

---

## 4. `use_count` is bumped atomically

The endpoints wrap the link lookup in `SELECT ... FOR UPDATE`
(`with_for_update(of=ShareLink)`), so two concurrent downloads against
a capped link can never both slip through when `use_count + 1 ==
max_uses`. Each download is a `use`; `use_count` becomes a plausible
audit signal of how often the link was exercised.

---

## 5. Frontend

`frontend/src/app/shared/[token]/page.tsx` fetches `/artifacts`
alongside `/info` on load. When `can_download` is true and there is at
least one report or document, a **Downloads** card appears under the
study metadata with one button per artifact. Password-protected links
show a hint telling the user to unlock the link before the buttons
work; the session JWT returned by `/verify` is kept in component state
and forwarded in the `Authorization` header for each download fetch,
then piped into an anchor click to save the blob.

Passwordless links skip the fetch-and-save dance and open the endpoint
in a new tab — the 307 redirect to the presigned URL streams the bytes
straight from the browser.

---

## 6. Files touched

- `backend/src/bvphoenix/api/sharing.py` — three new endpoints
  (`/artifacts`, report download, document download) plus a shared
  `_load_valid_link` helper.
- `backend/src/bvphoenix/services/permissions.py` — `SHARED_DOWNLOAD`
  alias.
- `frontend/src/app/shared/[token]/page.tsx` — Downloads card and per-
  artifact buttons.
- `docs/shared-link-downloads.md` — this document.
