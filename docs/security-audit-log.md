# Security audit log

Healthcare platforms need a defensible answer to "who looked at this
patient's data, when, from where, under which grant". Unit S7 wires the
existing `audit_log` table (see
`backend/src/bvphoenix/db/models/audit.py`) to every sensitive HTTP
endpoint and ships an admin-only read API at `GET /api/audit`.

## Architecture

```
API endpoint ──(Depends)──▶ AuditContext.log(...)
                                   │
                                   ▼
                     services.audit.log_action
                                   │
                                   ▼
                   SessionFactory()   ← separate txn (append-only)
                                   │
                                   ▼
                        audit_log (Postgres)
```

- **`services/audit.py`** is the single write path. It opens its own
  `SessionFactory()` session so audit rows are not rolled back with
  the caller's transaction. Audit writes are fire-and-forget: any
  failure logs a warning and re-raises nothing.
- **`middleware/audit_dependency.py`** is a FastAPI dependency that
  captures the incoming `Request` and exposes a bound
  `await audit.log(...)` coroutine. Endpoints use the
  `AuditDep` alias:

  ```python
  from bvphoenix.middleware.audit_dependency import AuditDep

  async def my_endpoint(..., audit: AuditDep) -> ...:
      ...
      await audit.log(
          action="study_view",
          actor_subject_id=user.subject_id if user else None,
          resource_kind="study",
          resource_id=study.id,
      )
  ```

- **`api/audit.py`** exposes `GET /api/audit` for admins. It supports
  query filters `actor`, `action`, `resource_kind`, `resource_id`,
  `from`, `to`, plus `limit` / `offset` pagination.

## PHI redaction

Metadata passed to `log_action` is scrubbed before insertion:

- Keys named `password`, `new_password`, `token`, `codice_fiscale`,
  `tax_id`, `display_name`, `full_name`, `first_name`, `last_name`,
  `birth_date`, `email`, `phone`, `address`, etc. are replaced with
  `"[redacted]"` at any nesting depth.
- Free-text values are scanned with a regex for Italian codice
  fiscale patterns and those matches are blotted out as
  `"[redacted-cf]"`.

This is a defense-in-depth safety net, not a full DLP pipeline.
Call sites should avoid putting raw PHI in metadata in the first
place; prefer resource IDs and structured field names.

## Action taxonomy

Actions are free-form strings, stored in `audit_log.action`
(`VARCHAR(64)`). The current instrumented vocabulary:

### Authentication (`api/auth.py`)

| Action           | Actor       | Resource kind | Notes                                    |
|------------------|-------------|---------------|------------------------------------------|
| `register`       | new user    | `user`        | Fired after the row commits              |
| `login_success`  | the user    | `user`        |                                          |
| `login_failed`   | none or user | `user`       | Records `email_attempted` in metadata    |
| `logout`         | the user    | `user`        | JWT is stateless, event is advisory      |
| `password_reset` | the user    | `user`        | Reserved for when reset flow lands       |

### Sharing (`api/sharing.py`)

| Action          | Actor              | Resource kind         | Notes                                               |
|-----------------|--------------------|-----------------------|-----------------------------------------------------|
| `share_create`  | grantor            | `study` / `patient`   | Logs grant id, target kind, access level, expiry    |
| `share_access`  | anonymous visitor  | `study` / `patient`   | Fired on successful `POST /shared/{token}/verify`   |
| `share_revoke`  | grantor            | `study` / `patient`   | Logs grant id + link id                             |
| `link_download` | anonymous visitor  | `instance`            | Use `instance_download` when the download lands     |

### Studies (`api/studies.py`)

| Action              | Actor           | Resource kind | Notes                                    |
|---------------------|-----------------|---------------|------------------------------------------|
| `study_view`        | viewer (or None)| `study`       | Fired on `GET /api/studies/{id}`         |
| `study_delete`      | owner           | `study`       | Reserved for when a delete endpoint lands |
| `instance_download` | viewer (or None)| `instance`    | Fired on the 307 redirect to S3          |

### Patients (`api/patients.py`)

| Action              | Actor        | Resource kind     | Notes                                           |
|---------------------|--------------|-------------------|-------------------------------------------------|
| `patient_view`      | viewer       | `patient`         |                                                 |
| `patient_update`    | editor       | `patient`         | Metadata lists which fields changed (keys only) |
| `patient_delete`    | owner        | `patient`         |                                                 |
| `document_upload`   | uploader     | `patient_document`| Metadata has `document_type`, `has_file`        |
| `document_download` | downloader   | `patient_document`| Reserved for future download endpoint           |

### Reports (`api/reports.py`)

| Action             | Actor     | Resource kind | Notes                             |
|--------------------|-----------|---------------|-----------------------------------|
| `report_create`    | author    | `report`      | Metadata has study_id, version    |
| `report_download`  | reader    | `report`      | Reserved for future download endpoint |

### A2A (`api/a2a.py`)

| Action               | Actor                 | Resource kind | Notes                                  |
|----------------------|-----------------------|---------------|----------------------------------------|
| `a2a_task_created`   | authed agent or None  | `a2a_task`    | Fired on `agent/sendMessage` new task  |
| `a2a_task_completed` | authed agent or None  | `a2a_task`    | Fired only on `completed` transitions  |

### MFA (`api/mfa.py`) — pending unit S5

| Action         | Actor     | Resource kind | Notes                         |
|----------------|-----------|---------------|-------------------------------|
| `mfa_setup`    | the user  | `user`        | Reserved for MFA endpoint     |
| `mfa_activate` | the user  | `user`        | Reserved for MFA endpoint     |
| `mfa_disable`  | the user  | `user`        | Reserved for MFA endpoint     |

### Versioning proposals (`api/proposals.py`)

| Action                      | Actor          | Resource kind | Notes                                                      |
|-----------------------------|----------------|---------------|------------------------------------------------------------|
| `proposal_conflict_resolve` | patient owner  | `proposal`    | Metadata: conflict_id, resolution kind, entity kind/id     |
| `proposal_merge`            | patient owner  | `proposal`    | Metadata: patient_id; one row per merge                    |
| `proposal_withdraw`         | proposer/owner | `proposal`    | Metadata: patient_id, reason                               |

### Versioning consultation (`api/consultations.py`)

| Action                | Actor         | Resource kind   | Notes                                  |
|-----------------------|---------------|-----------------|----------------------------------------|
| `consultation_sign`   | signer        | `consultation`  | Metadata: patient_id, optional note    |
| `consultation_create` | author        | `consultation`  | Reserved for create endpoint expansion |

### GDPR (`api/gdpr.py`)

| Action                  | Actor             | Resource kind | Notes                                            |
|-------------------------|-------------------|---------------|--------------------------------------------------|
| `gdpr.erasure_executed` | None (background) | `user`        | Metadata: request_id, scope, counters dict       |

When adding a new action name, prefer `<resource>_<verb>` and keep it
under 64 characters.

### Source-pinned coverage

A regression in this taxonomy would be invisible at runtime — an
endpoint that drops the `audit.log(...)` call still returns 200, just
without an audit row. To keep the contract honest, the test
`tests/test_versioning_authz_concurrency.py::TestAuditLogCompleteness::test_proposals_endpoints_emit_audit_log_calls`
inspects the source of every privileged proposal endpoint with
`inspect.getsource()` and asserts that the literal `audit.log` is
present. Apply the same pattern when adding a new privileged
endpoint elsewhere — the test is short enough to copy and the failure
mode is loud:

```python
def test_endpoints_emit_audit_log_calls() -> None:
    import inspect
    from bvphoenix.api import proposals as mod
    for name in ["resolve_conflict", "merge_proposal", "withdraw_proposal"]:
        assert "audit.log" in inspect.getsource(getattr(mod, name))
```

## Reading the audit log

```bash
# Last 50 login failures
GET /api/audit?action=login_failed&limit=50

# Everything an admin did in a window
GET /api/audit?actor=<subject_id>&from=2026-01-01T00:00:00Z&to=2026-02-01T00:00:00Z

# Who accessed a specific patient
GET /api/audit?resource_kind=patient&resource_id=<uuid>
```

The endpoint is restricted to admins via
`bvphoenix.auth.require_admin`.

## Operational notes

- **Separate transaction.** The audit row lands even if the business
  transaction rolls back. This is intentional: an attempted-but-failed
  action is worth logging.
- **No retries.** A failed audit write emits a `warning`-level log
  line under the `bvphoenix.audit` logger. Operators should monitor
  that channel. We deliberately avoid retry logic to keep latency
  predictable and prevent runaway queues if Postgres is unhealthy.
- **Client IP.** The service honours `X-Forwarded-For` (first hop) and
  `X-Real-IP` when present. In the infra manifest (`docs/` + traefik
  / nginx front door), these headers are safe to trust.
- **Retention.** Rows are append-only. Retention policy is an
  operational concern handled out-of-band; the schema does not expire
  rows automatically.
