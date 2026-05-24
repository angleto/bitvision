# Model Registry, operational runbook

Operational runbook for the embedding model registry in
bit.vision phoenix. Covers adding new models, re-indexing
existing embeddings, multi-model coexistence, rollback, and
fault tolerance.

---

## Concepts

- **`embedding_models`**: catalog table of available models.
  Each row: `id`, `name`, `kind` (`image` / `text` / `multimodal`),
  `dim`, `provider`, `weights_uri`, `is_active`, `is_default_for_kind`,
  `created_at`, `deprecated_at`.
- **CLI** `bvphoenix-embed-models`: manages the lifecycle:
  `register`, `list`, `activate`, `deprecate`.
- **Dispatch**: the `embed_series` worker and the search endpoints
  (`/api/similar-to/{id}`, `/api/search/*`) read the registry and
  pick the model based on `kind` and `is_default_for_kind`. You
  can force a specific model by passing `model=<name>` as a
  query param.

The registry is the source of truth: changing the default embedding
does not require a code deploy, only an atomic CLI command.

---

## Adding a new model (e.g. biomedclip-v2)

Full procedure to introduce a new model in production without
downtime and with safe rollback.

### 1. Deploy weights
Download the weights from the provider (HuggingFace, OpenAI, local) or
upload the artifacts to S3 if self-hosted. Verify
`weights_uri` is accessible from the workers (IAM permissions, private network).

### 2. Register in the catalog
```bash
bvphoenix-embed-models register \
  --name biomedclip-v2 \
  --kind image \
  --dim 512 \
  --provider biomedclip \
  --weights-uri hf:microsoft/biomedclip-v2
```
The model enters the registry with `is_active=false`,
`is_default_for_kind=false`: invisible to dispatch until it is
activated.

### 3. Dry-run test
```bash
bvphoenix-reembed start \
  --target-kind series \
  --to-model biomedclip-v2 \
  --dry-run
```
Returns the count of targets that would be re-indexed without
writing anything. Verify the number is plausible.

### 4. Production re-index
```bash
bvphoenix-reembed start \
  --target-kind series \
  --to-model biomedclip-v2 \
  --from-model biomedclip-v1 \
  --batch 100
```
The job processes targets in batches of 100; each batch is a transaction.
The job produces a `job_id` printed to stdout.

### 5. Monitor
```bash
bvphoenix-reembed status <job-id>
```
Or via UI: `/admin/embeddings` shows live progress bar, failure
rate, ETA.

### 6. Activate as default
When coverage reaches **>= 95%** of the target corpus:
```bash
bvphoenix-embed-models activate \
  --id biomedclip-v2 \
  --default-for-kind image
```
From this point on all new embeds and default queries use
`biomedclip-v2`. The old `biomedclip-v1` remains queryable via
`?model=biomedclip-v1` until it is deprecated.

### 7. Deprecate the old model (optional)
After an observation period in production (suggested: >= 1 week
without regressions):
```bash
bvphoenix-embed-models deprecate --id biomedclip-v1
```
Deprecate sets `deprecated_at` and removes the model from active dispatch.
Embedding rows remain in the table: only the model is no longer
selectable from dispatch.

---

## Rollback

If a re-indexing shows regressions (quality drop, high failure rate),
you can roll back:

```bash
bvphoenix-reembed rollback <job-id>
```

Rollback:
- Deletes the embedding rows **created by the indicated job**
  (tracked via `job_id` in the `embeddings` table)
- Does not touch the old model, which stays `is_default_for_kind=true`
  until the new one has been activated
- Is idempotent: re-running it is a no-op

If the new model has already been activated as default before discovering
the problem: first reactivate the old one
(`bvphoenix-embed-models activate --id biomedclip-v1 --default-for-kind image`),
then roll back the job.

---

## Multi-model coexistence

Multiple models can be `is_active=true` at the same time. Unique
constraint: **only one** is `is_default_for_kind=true` per (kind) at any
moment (enforced via DB constraint).

Supported scenarios:
- **A/B test**: two active models, default on one, the other
  queryable via explicit `?model=<name>`
- **Gradual transition**: keep the old model active during
  the re-indexing of the new one, so old-model queries continue to
  work
- **Multi-kind**: an `image` model and a `text` model as default
  at the same time (different kinds, independent constraints)

The search endpoint accepts `model=<name>` as an explicit override, useful
for client-side comparisons or offline validation.

---

## Fault tolerance

- **Embed errors** are recorded in the `embedding_errors` table
  (`target_kind`, `target_id`, `model_id`, `error_message`,
  `occurred_at`, `retry_count`)
- **UI** `/admin/embeddings` shows retries, failure rate, breakdown by
  error type
- **Failure rate gating**: `bvphoenix-reembed start` accepts
  `--skip-failed-after 5` which stops the job if the failure rate exceeds
  5%, avoiding wasted compute on a broken batch (e.g. weights URI
  unreachable, systematic OOM)
- **Retry policy**: transient errors (S3 timeout, provider rate limit)
  are retried automatically with exponential backoff
  up to 3 attempts before being written to `embedding_errors`

---

## References

- Search + embeddings overview: [search-and-embeddings.md](./search-and-embeddings.md)
- Data model: [data-model.md](./data-model.md)
- Agent protocols (MCP tool `semantic_search`, `search_hybrid`):
  [agent-protocols.md](./agent-protocols.md)
