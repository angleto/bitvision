# Agent protocols — MCP + A2A

Bitvision phoenix exposes three complementary surfaces for LLM and
agent integrations:

1. **MCP (Model Context Protocol)** — stdio JSON-RPC server that lets an
   individual LLM (Claude Desktop, an IDE, a custom agent) call tools on
   behalf of a single user
2. **A2A (Agent-to-Agent Protocol)** — HTTP JSON-RPC endpoint that lets
   autonomous agents (e.g. "doctor agents" running elsewhere) delegate
   tasks as peers, with a task lifecycle and multi-turn conversations
3. **REST API** — the underlying FastAPI surface. Both MCP and A2A
   delegate to it, so visibility rules (grants, RLS) apply uniformly.

This document reflects what is actually implemented today. Items marked
**_planned_** are scoped but not yet landed.

## When to use which

| Goal | Use |
|------|-----|
| An LLM acting on behalf of a logged-in user | **MCP** |
| An autonomous agent running its own workflow against bitvision | **A2A** |
| Programmatic integration from a trusted backend | **REST API** directly |
| Subscribing to events | not yet — webhooks are on the roadmap |

---

## 0. LLM consultation workflow (primary use case)

The flagship flow bitvision phoenix is built around: a clinician
delegates a structured review of a patient fascicolo to an LLM, which
cross-reads studies, referti, documenti clinici, and returns a
grounded consultation with citations back to the source records.

Two shapes:

1. **Interactive (Claude Desktop via MCP)** — the clinician asks
   "analizza il fascicolo di X"; Claude calls MCP tools
   (`get_fascicolo_bundle`, `get_patient_timeline`, `list_reports`,
   `get_series_thumbnail`), forms a plan, and writes back into the
   thread. Optionally `create_consultation` persists the result as a
   `Consultation` record linked to the patient.
2. **Headless (agent + Anthropic API)** — a scheduled or
   user-triggered script pulls the bundle, composes a multimodal
   prompt (text + image blocks), invokes Claude via the Anthropic API,
   parses the response, and POSTs back a `Consultation`. See the
   end-to-end reference in
   [`examples/fascicolo_consultation/`](../examples/fascicolo_consultation/).

The flow supports several consultation patterns — triage,
second-read, cohort retrieval, longitudinal summary, patient-facing
plain-language explain.

Entry points for integrators:

- Clinician pointing Claude Desktop at their fascicolo:
  [claude-desktop-quickstart.md](./claude-desktop-quickstart.md) —
  5-minute setup.
- Python script consuming REST + Anthropic:
  [`examples/fascicolo_consultation/agent.py`](../examples/fascicolo_consultation/agent.py).
- Autonomous agent invoking the `radiology-consultation` A2A skill:
  [§2](#2-a2a-agent-to-agent-protocol) below.

---

## 1. MCP Server

### Layout

```
mcp/
├── src/bvmcp/
│   ├── server.py                # stdio entry point: list_tools + call_tool
│   ├── server_http.py           # HTTP transport (ADR 0019, per-assistant bearer)
│   ├── auth.py                  # bearer-hash gate + TTL cache
│   ├── oauth_shim.py            # MCP-spec OAuth metadata for HTTP clients
│   ├── _tool_registry.py        # dispatch table, populated from tools/*
│   ├── scopes.py                # OAuth-style scope catalogue
│   ├── feature_flags.py
│   ├── config.py                # BVP_MCP_BACKEND_BASE_URL, BVP_MCP_USER_TOKEN, ...
│   └── tools/                   # one module per family (see catalogue below)
└── tests/
```

### Tool catalogue

The source of truth for the catalogue is the registry built from
`mcp/src/bvmcp/tools/`. Fixing a count in this document made it drift
the moment a new family landed; we now list the **families** instead
of repeating numbers that grow every sprint. Open a directory listing
of `mcp/src/bvmcp/tools/` for the live inventory; each module is a
small file with the tool definitions and a short docstring.

| Family (module) | Coverage | Backed by |
|---|---|---|
| `search.py`, `search_advanced.py` | Full-text + structured filters, similarity, semantic, hybrid (RRF) | `/api/search*`, `/api/similar-to/{id}` |
| `studies.py`, `images.py`, `imaging.py`, `segmentations.py` | Study / series detail, thumbnails, volume packing, MPR slice + ROI crop, DICOM metadata allowlist, distance / volume measurements, SUV, segmentation records, registrations | `/api/studies`, `/api/series/*`, `/api/segmentation-records`, `/api/registrations` |
| `patients.py`, `patient_writes.py`, `bundle.py`, `provenance.py`, `external_identifiers.py` | Patient detail, fascicolo index / timeline / bundle, contacts CRUD, telegram link, external-id link, provenance, scope introspection | `/api/patients/*`, `/api/me/scopes` |
| `documents.py`, `document_reads.py`, `document_writes.py` | List / get / update / merge / delete / restore documents; document-study links; binary download URL; OCR text; entity extraction; bulk update | `/api/patients/{pid}/documents*`, `/api/jobs/{id}` |
| `care_phases.py`, `clinical_events.py`, `clinical_event_attachments.py` | Propose / apply / create / update / delete / reorder / restore care phases; clinical event CRUD; phase-event assignment; ICS / PDF / SVG / Markdown export | `/api/patients/{pid}/care-phases*`, `/api/patients/{pid}/clinical-events*` |
| `folders.py`, `patient_tasks.py`, `notifications.py`, `calendar_subscriptions.py` | Folder CRUD + hardlinks, patient tasks with reminders, contact channels + opt-out, calendar subscriptions / ICS feeds | `/api/folders*`, `/api/patient-tasks*`, `/api/notifications*`, `/api/calendar*` |
| `annotations.py`, `tags.py`, `metadata_writes.py`, `clinical_notes.py`, `report_contents.py` | Read + write annotations, tags (`tags:write`), study / series metadata (`studies:write_metadata`, `series:write_metadata`), clinical notes, report content versions | `/api/markers`, `/api/tags*`, `/api/studies/{id}*`, `/api/clinical-notes*`, `/api/report-contents*` |
| `summaries.py`, `qna.py`, `entities.py`, `labs.py`, `help.py` | LLM summaries, Q&A, document entities, lab timeseries, `bvphoenix help` introspection | `/api/summaries*`, `/api/qna*`, `/api/labs*` |
| `sharing.py` | Mint and revoke study / folder share links with optional `idempotency_key` | `/api/studies/{id}/share`, `/api/folders/{id}/share-links` |

The MCP layer is a **strict superset of the GUI** (memory
`feedback_mcp_must_be_gui_superset`): every action a user can take in
the Health Record has at least one matching tool, plus a few
agent-only utilities (`get_my_scopes`, batch helpers).

Cross-patient invariant (memory `cross_patient_links_forbidden`):
every patient-scoped tool requires `patient_id` as its first argument,
and there is **no tool that takes two `patient_id`s**. Phase / event /
document ids that belong to a different patient surface as `404` from
the backend (composite FK + nested REST routes).

### Care phases (semantic timeline)

Source: `mcp/src/bvmcp/tools/care_phases.py`. Conceptual model in
[`fascicolo.md`](./fascicolo.md), full spec in
[`care-timeline-phases.md`](./care-timeline-phases.md). Registered in
`mcp/src/bvmcp/server.py` `_TOOL_MODULES`.

**Cross-patient invariant.** Every tool requires `patient_id` as the
first input. Any `phase_id`, `event_id` or `revision_no` that belongs
to a different patient resolves to `404`: the composite FK on
`care_phase` plus per-request scoped queries make cross-patient
operations unrepresentable end-to-end (DB → service → tool).

**Scopes.** Three new scopes are added to the existing scope catalog
(`mcp/src/bvmcp/scopes.py`), grantable independently through the
`/settings/ai-assistants` UI:

- `phases:read`: read phases, material, revisions, rendered timeline.
- `phases:propose`: invoke the LLM classifier (dry-run only; no
  state change beyond a cached `care_phase_proposal` row).
- `phases:write`: apply proposals, create / update / delete phases,
  assign and unassign events, reorder, restore.

The MCP layer is a strict superset of the GUI: every
button or affordance in the Health Record's care-timeline tab has a
matching MCP tool, plus a few introspection / batch tools that exist
only on the MCP side. See memory `feedback_mcp_must_be_gui_superset`.

| Scope | Tool | Inputs | Effect |
|-------|------|--------|--------|
| `phases:read` | `get_care_timeline` | `patient_id`, `lang?` (`it|en`), `format?` (`json|markdown|svg`) | Bundle in one call. SVG matches the reference style. |
| `phases:read` | `render_care_timeline_svg` | `patient_id`, `lang?`, `width?`, `theme?` (`light|dark`) | SVG-only alias with rendering knobs. |
| `phases:read` | `get_care_phase` | `patient_id`, `phase_id`, `include?` | Phase detail with assigned events. |
| `phases:read` | `list_care_phases` | `patient_id` | Cheap chip list (counts only, no event details). Cheaper than `get_care_timeline` when the agent only needs the chips. |
| `phases:read` | `list_care_phase_material` | `patient_id`, `phase_id` | Grouped material with `mcp://` URIs (no presigned S3 URLs leak). |
| `phases:read` | `list_care_phase_revisions` | `patient_id`, `phase_id` | Revision history (audit + restore points). |
| `phases:read` | `get_care_timeline_health` | `patient_id` | Salute-panel snapshot: `n_phases`, `n_events`, `pct_assigned`, `pending_proposals`, `last_classifier_run`. Drives the GUI salute panel and lets agents decide whether to re-propose. |
| `phases:read` | `export_care_timeline_ics` | `patient_id`, `lang?` | iCalendar (RFC 5545); one `VEVENT` per clinical event with deterministic `UID` and phase slug as `CATEGORIES`. Returned as `TextContent` with `_meta.mimeType='text/calendar; charset=utf-8'`. |
| `phases:read` | `export_care_timeline_pdf` | `patient_id`, `lang?` | Stub: returns a structured `not_implemented` JSON until `weasyprint` is bundled. The tool name is published now so the GUI button and any agent share the contract. |
| `phases:propose` | `propose_care_phases` | `patient_id`, `lang?`, `dry_run=true` | Runs / cache-hits the classifier. Returns a proposal; no DB mutation on `care_phase`. |
| `phases:write` | `apply_phase_proposal` | `patient_id`, `proposal_id`, `accept_phases`, `accept_assignments`, `idempotency_key` | Atomic apply. `Idempotency-Key` mandatory. |
| `phases:write` | `create_care_phase` | `patient_id`, `slug`, `name_i18n`, `kind`, `color_hex?`, `ordinal?`, `narrative_md?` | Manual create (`author_kind='agent'`). |
| `phases:write` | `update_care_phase` | `patient_id`, `phase_id`, `etag`, `patch` | `If-Match` mandatory; `412` on mismatch. |
| `phases:write` | `delete_care_phase` | `patient_id`, `phase_id` | Hard delete. Composite FK `ON DELETE SET NULL` orphans assigned events (`phase_id = NULL`). Revision row of kind `delete` appended. |
| `phases:write` | `assign_event_to_phase` | `patient_id`, `phase_slug`, `event_id`, `confidence?` | Sets `event.phase_id` plus assignment provenance. |
| `phases:write` | `unassign_event_from_phase` | `patient_id`, `event_id` | Sets `event.phase_id = NULL`. |
| `phases:write` | `reorder_care_phases` | `patient_id`, `ordinals` | Batch ordinal update. |
| `phases:write` | `restore_care_phase_revision` | `patient_id`, `phase_id`, `revision_no` | Restore from `care_phase_revision`. |
| `patients:read` | `get_my_scopes` | _none_ | Caller introspection: subject id, admin flag, scope set, agent-token id when present. The only care-phase tool not patient-scoped (it asks "who am I?"). Maps to `patients:read` as the lowest-privilege scope already required for any session. |

`kind` accepted values: `imaging`, `surgery`, `followup`,
`surveillance`, `visit`, `reassessment`, `other`. Every write goes
through the audit log with `author_kind='agent'` and the agent
assistant id, so AI provenance remains visible after a human accepts
or amends the change.

Cross-patient invariant: every tool except `get_my_scopes` requires
`patient_id` as the first input. Phase / event ids that belong to a
different patient surface as `404` from the backend (composite FK +
nested REST routes); the MCP layer never silently rewrites the
response. There is no tool that takes two `patient_id`s.

### How it runs

1. An MCP client launches the server: `uv run --project mcp python -m bvmcp.server`
2. The server reads `BVP_MCP_BACKEND_BASE_URL` and `BVP_MCP_USER_TOKEN`
   from env
3. Each tool call becomes an HTTP call to the backend with that Bearer
   token, so the same visibility/grant rules apply

### Claude Desktop setup

```json
{
  "mcpServers": {
    "bitvision": {
      "command": "uv",
      "args": ["run", "--project", "/absolute/path/to/bitvision_phoenix/mcp", "python", "-m", "bvmcp.server"],
      "env": {
        "BVP_MCP_BACKEND_BASE_URL": "http://localhost:8000",
        "BVP_MCP_USER_TOKEN": "<jwt from /api/auth/login>"
      }
    }
  }
}
```

Smoke test: tell Claude "list my recent studies" — it will call
`search_studies` and return results.

For a guided walkthrough (create token, paste config, first prompt,
troubleshooting), see
[claude-desktop-quickstart.md](./claude-desktop-quickstart.md) —
5 minuti end-to-end.

---

## 2. A2A (Agent-to-Agent Protocol)

### Protocol shape

A2A v1.0, JSON-RPC 2.0 over HTTP, Bearer JWT auth (same token as REST).

- **Agent Card** discovery: `GET /.well-known/agent-card.json`
  (declarative capabilities; see `backend/src/bvphoenix/api/a2a.py`)
- **Task endpoint**: `POST /api/a2a` accepts all JSON-RPC methods below

### JSON-RPC methods

| Method | Purpose |
|--------|---------|
| `agent/sendMessage` | Create a task or append a message to an existing one |
| `agent/getTask` | Current state of a task |
| `agent/listTasks` | List tasks (optionally filtered by `contextId`) |
| `agent/cancelTask` | Cancel a task |
| `agent/getAgentCard` | Authenticated variant of the Agent Card |

### Skills

| Skill id | Status | What it does |
|----------|--------|--------------|
| `dicom-search` | ✅ Real execution | Full-text + metadata search via `/api/search` |
| `similarity-search` | ✅ Real execution | BiomedCLIP similarity via `/api/similar-to/{id}` |
| `patient-fascicolo` | ✅ Real execution | Fascicolo index + timeline |
| `image-analysis` | ✅ Real execution | LLM-generated series description (persists as annotation) |
| `radiology-consultation` | ✅ Real execution — persists `Consultation` (C4) | Multi-step: extracts study id, runs similarity + analysis, **persists a `Consultation` row** linked to the patient/study (replaces the previous stub-artifact flow) |
| `fascicolo-executive-summary` (U5) | _planned_ | Executive summary su tutto il fascicolo paziente: aggrega timeline + referti + documenti + consultazioni precedenti e produce un brief strutturato (status clinico, trend, open questions) |

Intent routing: `backend/src/bvphoenix/services/a2a_intent.py` uses the
configured LLM provider (Anthropic when `BVP_LLM_PROVIDER=anthropic` and
`BVP_ANTHROPIC_API_KEY` is set) to parse natural language into
`(skill_id, params)`. When the LLM is unavailable or returns an error,
a keyword heuristic (matching `similar`, `patient`, UUID extraction,
etc.) takes over so `/api/a2a` never 500s on intent parsing.

### Task lifecycle

```
SUBMITTED → WORKING → COMPLETED
                   → INPUT_REQUIRED → (client replies) → WORKING → …
                   → FAILED
                   → CANCELED
```

### Task persistence

`backend/src/bvphoenix/services/a2a_store.py` — Redis-backed
(`BVP_REDIS_URL`), 7-day TTL, `a2a:task:<id>` keys with per-context
sets. Falls back transparently to an in-process dict when Redis is
unreachable, so tests and single-process dev setups still work.

### Example — similarity consultation

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "agent/sendMessage",
  "params": {
    "message": {
      "role": "user",
      "parts": [
        {"type": "text", "text": "find similar cases to 3c7b2f41-1e28-4d04-9c63-0d7c2a5f8b90"}
      ]
    }
  }
}

// Response (excerpt)
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "id": "task-uuid",
    "status": {"state": "completed"},
    "artifacts": [
      {
        "name": "response",
        "parts": [
          {"type": "text", "text": "Top 5 similar studies: …"},
          {"type": "data", "data": [{"study": {…}, "score": 0.94, …}]}
        ]
      }
    ]
  }
}
```

### Reference client

A minimal runnable example lives in `examples/doctor_agent/`. It
reads the Agent Card, sends `agent/sendMessage`, polls until the task
reaches a terminal state, handles `INPUT_REQUIRED` by prompting on
stdin, and pretty-prints artifacts. Run:

```bash
cd examples/doctor_agent
uv sync
uv run python agent.py --token "$JWT" --query "find similar cases to <study-id>"
```

---

## 3. Visual similarity search pipeline

### Embedding generation (BiomedCLIP)

Worker task `workers/src/bvworkers/tasks/embed_series.py`:

1. Fetches the middle-slice instance from S3
2. Decodes DICOM pixel data → PIL RGB Image
3. Runs BiomedCLIP (`microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224`)
   visual encoder → 512-dim vector
4. L2-normalizes for cosine similarity
5. Upserts into `embeddings` (pgvector) with `model_id='biomedclip-v1'`

Requires the `ai` extra: `cd workers && uv sync --extra ai`.

### Storage + index

- Table: `embeddings` (see `backend/src/bvphoenix/db/models/embeddings.py`)
- Unique on `(target_kind, target_id, model_id)`
- pgvector HNSW cosine index (see `alembic/versions/0002_*.py`)
- Larger-dimensional models get their own table — pgvector is fixed-dim per column

### Query path

`GET /api/similar-to/{target_id}?k=10&modality=CT` (see
`backend/src/bvphoenix/api/search.py:114-238`):

1. Resolve target to a series embedding (accepts study ids too — uses
   the first embedded series of the study)
2. pgvector `<=>` cosine distance to fetch k×3 candidates
3. Filter by visibility (`visible_studies_filter`) and optional
   modality
4. Return top-k unique studies with `score = 1 - cosine_distance`

### Trigger embedding

- `POST /api/series/{id}/embed` — enqueues the Arq job, returns 202
- MCP `embed_series` tool wraps the same endpoint

### Frontend surfaces

- `/search/visual` — dedicated visual search page (pick reference → see
  nearest neighbors)
- Study detail page — "Similar cases" panel calls `/api/similar-to/{studyId}`
- Series viewer — compact sidebar panel scoped to the current series

---

## 4. Authentication

- Login: `POST /api/auth/login` returns a JWT
- All agent protocols use the same token:
  - REST + A2A: `Authorization: Bearer <token>` header
  - MCP: `BVP_MCP_USER_TOKEN` env var; the server attaches it to every
    backend call

Permissions are enforced in `backend/src/bvphoenix/services/permissions.py`
using grants (study/patient/resource) + role-based checks. The same
rules apply regardless of which surface the caller came through.

---

## 5. AI assistants and agent tokens

Session JWTs (from `/api/auth/login`) work for MCP and A2A but carry
the full user scope and a short TTL bound to browser sessions. For
agents — Claude Desktop, headless scripts, long-running workers — we
issue **agent tokens** via the per-user **AI assistant** model.

The full design lives in [`ai-assistants.md`](./ai-assistants.md);
the short version: an *assistant* is a per-user identity (label,
provider, model_id, permissions) with at most one active token at a
time and an N–N list of patients shared with it. Rotating a token
preserves the assistant identity + patient list, so the user only
swaps the bearer string in their MCP client config.

### Surface

Mounted under `/api/ai-assistants` (see
`backend/src/bvphoenix/api/ai_assistants.py`):

- `POST /api/ai-assistants` — create assistant + mint first token.
  Body: `{label, provider?, model_id?, notes?, permissions: [...],
  deidentify_on_use?, ttl_seconds?}`. The raw JWT is in the
  response **exactly once**.
- `GET /api/ai-assistants` — list the user's assistants with the
  active-token summary (token mask, expiry) + patient count.
- `PATCH /api/ai-assistants/{id}` — edit metadata + permissions.
- `DELETE /api/ai-assistants/{id}` — cascade delete (token + patient
  links).
- `POST /api/ai-assistants/{id}/rotate` — revoke current token, mint
  new one. Response carries the raw JWT.
- `GET / POST /api/ai-assistants/{id}/patients`,
  `DELETE /api/ai-assistants/{id}/patients/{patient_id}` — manage
  the patient share list.

### Permissions

The whitelist is narrow on purpose:
`patient:read`, `patient:images`, `consultation:read`,
`consultation:write`. Unknown verbs are 400'd before any DB write so
a typo in the caller doesn't silently mint a token nobody understands.
At runtime the backend resolves `(token → assistant → patient set)`
once per request and gates every patient-scoped chokepoint via
`enforce_agent_patient_scope` (set membership, O(1)).

### UI

Two surfaces:

- **`/settings/ai-assistants`** — list / create / rotate / delete
  assistants; expand a row to manage its patient list.
- **"Share with AI"** button on the Health Record — small modal
  (`ShareWithAiModal`) with a checkbox per assistant. Same patient
  can be shared with multiple assistants for benchmark / second-
  opinion workflows.

See [claude-desktop-quickstart.md](./claude-desktop-quickstart.md)
for the clinician-facing walkthrough.

### De-identification

When `deidentify=true` is set on token creation, responses passing
through the token strip or generalize direct identifiers (name,
`tax_id`, exact DOB, address, contacts) before serialization.
Pixel-level burned-in PHI is already removed upstream by the DICOM
worker; the token flag covers structured metadata. Clinical content
(modality, findings, study date approx., BiomedCLIP embeddings)
remains intact.

---

## 6. Limitations & roadmap

- **No streaming on A2A yet.** `capabilities.streaming` in the Agent
  Card is advertised conservatively. `agent/getTask` polling is the
  current flow; SSE upgrade is planned.
- **No outbound push to agents yet.** The notification dispatcher
  (`docs/notifications.md`) covers patient-contact channels (email,
  Telegram, generic webhook); A2A push to agents is not wired.
- **LLM intent parser calls Anthropic synchronously** — no batching,
  no caching of recurrent phrasings. A small prompt cache is on the
  roadmap.
- **Authorization** uses application-layer predicates
  (`services/permissions.py`) **plus** PostgreSQL RLS on the F12
  versioning tables and on the core resource tables. Production
  hardening still pending: RLS is decorative when the app role
  coincides with the table owner (Postgres bypasses RLS for owners
  unless `FORCE ROW LEVEL SECURITY` is enabled). See
  [security-rls.md §"Production hardening required"](./security-rls.md)
  for the two paths (separate app role vs. forced RLS).
- **Audit logging** is wired on every privileged endpoint touched in
  the 2026-05-21 security pass (see
  [security-2026-05-21-assessment.md](./security-2026-05-21-assessment.md))
  and is **source-pinned by tests** (see
  [security-audit-log.md §"Source-pinned coverage"](./security-audit-log.md)).
  Newly-added privileged endpoints must follow the same pattern.
- **Task persistence** is Redis-backed with a flat 7-day TTL; no
  archive-to-cold-storage path yet.

These are tracked as scheduled follow-up batches in the project's
internal planning notes.

---

## References

- [MCP specification](https://modelcontextprotocol.io/)
- [A2A protocol](https://a2a-protocol.org/latest/specification/)
- [A2A on GitHub](https://github.com/a2aproject/A2A)
- Implementation: `backend/src/bvphoenix/api/a2a.py`,
  `backend/src/bvphoenix/services/a2a_intent.py`,
  `backend/src/bvphoenix/services/a2a_store.py`
- Clinician quickstart (5 min): [claude-desktop-quickstart.md](./claude-desktop-quickstart.md)
- Search + embeddings (semantic_search, search_hybrid, summarize): [search-and-embeddings.md](./search-and-embeddings.md)
- Tag taxonomy (namespaces, auto-tagging, sinonimi): [tag-taxonomy.md](./tag-taxonomy.md)
- Model registry runbook: [model-registry.md](./model-registry.md)
- A2A reference client: [`examples/doctor_agent/`](../examples/doctor_agent/)
- End-to-end multimodal consultation: [`examples/fascicolo_consultation/`](../examples/fascicolo_consultation/)
