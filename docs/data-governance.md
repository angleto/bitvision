# Data governance dossier

This is the auditable description of how bitvision phoenix handles
patient data: what is de-identified, how cohorts are k-anonymized, what
the patient can do with their record, and what is logged. It is the open
counterpart to a closed, irreversible institutional black-box: the
differentiator is **auditability + reproducibility**, not a sealed
patent.

The machine-readable, versioned form of this dossier is served publicly
at **`GET /api/governance`** (`policy_version` + the runtime-sourced
values). Aggregate platform stats live at `GET /api/transparency`.

## Honest framing (load-bearing)

bitvision applies **pseudonymization + tiering + k-anonymity + auditable
redaction**. It is **not** irreversible anonymization, and we never claim
parity with it. The platform can re-identify within its own trust
boundary; that is the point of a *patient-owned* record (the patient gets
their data back, can prove and revoke consent, and can be erased). The
one axis where an institutional lake is strong (irreversible one-way
anonymization) is the axis it is closed on; we differentiate by being
open and patient-sovereign instead of competing on irreversibility.

## De-identification passes

| Surface | Module | What it does |
| --- | --- | --- |
| Clinical-note text | `services/deid_text.py` | Regex passes for Italian tax code, email, phone, precise dates, addresses (`_KIND_TO_PATTERN`), plus an optional LLM scrub. Each redaction is recorded in `redaction_events` with the model/provider when an LLM ran. |
| DICOM tags | `services/deidentify.py` | Header de-identification aligned to the PS3.15 Basic Application Level Confidentiality Profile (in-house, table-driven). |
| Burned-in pixels | `services/pixel_deid.py`, `pixel_deid_eval.py` | Conservative gate on the public path; OCR + regex redaction tiers. Hardening is ongoing (see the anonymizer epic). |
| Faces | `services/face_deid.py` | Face de-identification for head/neck volumes before public release. |
| Pathology WSI | `services/wsi_deid.py` | Label / macro images carrying scanner-printed PHI are de-identified before public release. |

The per-study **text** de-identification record is itself public for an
OpenData study: `GET /api/studies/{id}/deidentification-provenance`
(category counts only, storage-isolated) with a frontend panel on the
study page.

## k-anonymity

Training cohorts are gated by `services/k_anonymity.py`: every
quasi-identifier bucket must contain at least `DEFAULT_K_MIN` (= 5)
studies, else the bucket is expanded or the build is refused. The applied
threshold is published in the policy endpoint so it cannot drift
unnoticed.

## Contribution tiers

`t1` private · `t2` shared (revocable links) · `t3` training opt-in
(de-identified + k-anonymized) · `t4` public (CC). A study only leaves
the patient's control by an explicit tier change.

## Patient rights

- **Ownership** — studies belong to the patient, not the institution.
- **Portability** — one-click [PHR-Bundle](phr-bundle.md) export (open,
  re-importable container) / GDPR Art. 20.
- **Erasure** — GDPR Art. 17 (`services/erasure.py`); public releases
  transfer ownership to an anonymous subject, audit entries are retained
  in redacted form as required.
- **Consent** — per-purpose (research / commercial / AI training),
  revocable; revocation propagates to future cohorts.

## Audit

Privileged and write actions are recorded in the audit log with the
actor (human or `agent`), action, resource, and metadata. AI-authored
content always carries `author_kind='agent'` and never appears as human.

## Reproducible deploy (quickstart)

The full stack is reproducible from the repo, no hidden state:

```
make up            # infra (Postgres+pgvector, MinIO, Redis) + backend + workers + migrate
make db.migrate    # alembic upgrade head (also seeds the catalog via 0001)
make backend.test  # the test suite, incl. the governance + PHR-Bundle conformance gates
```

Images are built per service from `infra/dockerfiles/<service>.Dockerfile`
and tagged from the git release tag (the tag, without the leading `v`, is
the image tag and what `GET /api/version` reports). The Kubernetes
manifests live in a separate deploy repo; `make deploy.apply` applies
them.
