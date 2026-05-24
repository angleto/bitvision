# Upload validation and public share info page

Two small hardening changes that complete earlier features:

1. A public landing page for share links that describes the share
   before the recipient enters a password (closes the gap where the
   backend exposed `GET /api/shared/{token}/info` but nothing in the
   frontend surfaced it as a preview).
2. MIME-type allow-lists and payload size limits on the two file upload
   endpoints (study reports and patient documents), returning the
   standard HTTP error codes `415` and `413`.

---

## 1. Public share info page

### Route

`frontend/src/app/shared/[token]/info/page.tsx` — rendered at
`/shared/<token>/info`.

This page is public: it never reads or writes a JWT and calls
`GET /api/shared/{token}/info` directly. The sibling route
`/shared/<token>` remains responsible for password entry and token
issuance.

### What it shows

Metadata returned by the info endpoint:

- Study title, modalities, study date.
- Whether a password is required.
- Expiry (with a visual note when already expired).
- Uses remaining (when the share has a `max_uses` cap).
- A summary of the granted permissions (e.g. `read_metadata`,
  `read_images`, `write_report`).

If the link is expired or exhausted, the action button is hidden and an
error banner is shown instead. Otherwise the page links through to
`/shared/<token>` with a button label that reflects whether a password
is required ("Enter password" vs "Open study").

### Backend change

`ShareInfoOut` in `backend/src/bvphoenix/api/sharing.py` was extended
with two optional fields:

- `max_uses: int | None` — the configured cap (or `None` for unlimited).
- `uses_remaining: int | None` — `max_uses - use_count`, clamped at 0.

Both are `None` for links without a use cap. The fields default to
`None` so existing clients keep working.

---

## 2. Upload validation

### Allow-list

The same set of MIME types is accepted by both endpoints — it matches
what a clinician would realistically attach to a study:

| MIME type                                                                   | Typical file |
| --------------------------------------------------------------------------- | ------------ |
| `application/pdf`                                                           | PDF referto  |
| `application/msword`                                                        | Legacy `.doc`|
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document`   | `.docx`      |
| `image/png`                                                                 | Scanned page |
| `image/jpeg`                                                                | Phone photo  |

Anything else is rejected with `415 Unsupported Media Type`.

### Size limit

50 MiB, enforced after the body is read into memory. Rejections return
`413 Request Entity Too Large`. The limit is a constant
(`DEFAULT_MAX_UPLOAD_MB`) so it can be raised centrally later if
whole-slide images or high-resolution PDFs start showing up.

### Helpers

`backend/src/bvphoenix/services/upload_validation.py` exposes two
reusable functions:

```python
from bvphoenix.services.upload_validation import validate_mime, validate_size

validate_mime(file.content_type)      # raises 415 if not in allow-list
data = await file.read()
validate_size(len(data))              # raises 413 if over 50 MB
```

Both raise `fastapi.HTTPException` with the documented status codes, so
the callers only need two extra lines each.

### Endpoints touched

- `POST /api/studies/{study_id}/reports` —
  `backend/src/bvphoenix/api/reports.py::create_report`.
- `POST /api/patients/{patient_id}/documents` —
  `backend/src/bvphoenix/api/patients.py::create_document`.

Both validate the `Content-Type` header before reading the request body
(fast rejection) and then validate the resulting byte length.

---

## Error shape

Both status codes use FastAPI's default JSON error envelope:

```json
{ "detail": "unsupported media type: application/x-msdownload" }
```

```json
{ "detail": "file exceeds maximum size of 50 MB" }
```

Frontend callers should surface `detail` to the user (the generic API
client already does so via `ApiError.message`).
