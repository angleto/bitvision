# mcp — Model Context Protocol server

Native MCP server that lets any MCP-compatible client (Claude Desktop,
Claude.ai custom connector, agents, IDEs) connect to a user's data on
bitvision phoenix.

## Stack

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- `httpx` — calls the backend REST API with user-scoped credentials
- Stateless: all authorization happens in the backend (RLS + grants +
  per-assistant scopes); the MCP process only carries the bearer
  forwarded by the client

## Two transports

- **stdio** (`bvmcp.server`) — Claude Desktop and other local stdio
  hosts launch this with the user's JWT in `BVP_MCP_USER_TOKEN`.
  Suitable for personal-laptop integrations.
- **HTTP** (`bvmcp.server_http`, ADR 0019) — long-running container
  behind an Ingress, accepts `Authorization: Bearer <client_secret>`
  emitted from Settings → AI assistants in the BitVision UI. Resolves
  the bearer via the in-cluster `/api/internal/agent-bearer/resolve`
  RPC and proxies the call to the backend. Suitable for Claude.ai
  custom connectors and any remote MCP host.

## Run

```sh
make mcp.install
make mcp.dev    # runs the stdio MCP server bound to BVP_MCP_USER_TOKEN
```

HTTP transport in dev:

```sh
uv run --project mcp python -m bvmcp.server_http
```

Production HTTP is the `mcp-http` container in
`deploy/bvphoenix-production-k8s-deploy/`. See ADR 0019 and
`docs/agents-api/onboarding-mcp.md` for the operator-side setup
(Telegram-bot–style reveal-once `client_id`/`client_secret`).

## Tools exposed

The catalogue is **registry-driven**: each module under
`src/bvmcp/tools/` registers one or more tools; the canonical list is
the directory listing. As of v3.8 the families are (see
`docs/agent-protocols.md` for the live narrative):

- `search.py`, `search_advanced.py` — text + structured + similarity +
  semantic + hybrid (RRF) search.
- `studies.py`, `images.py`, `imaging.py`, `segmentations.py` — study
  / series detail, thumbnails, volume packing, MPR slice + ROI crop,
  DICOM metadata allowlist, distance / volume measurements, SUV,
  segmentation records, registrations.
- `patients.py`, `patient_writes.py`, `bundle.py`, `provenance.py`,
  `external_identifiers.py` — patient detail, fascicolo index /
  timeline / bundle, contacts CRUD, external-id link, scope
  introspection.
- `documents.py`, `document_reads.py`, `document_writes.py` — list /
  get / update / merge / delete / restore documents; document-study
  links; signed binary download URL; OCR text; entity extraction;
  bulk update.
- `care_phases.py`, `clinical_events.py`,
  `clinical_event_attachments.py` — propose / apply / create / update
  / delete / reorder / restore care phases; clinical event CRUD;
  phase-event assignment; SVG / Markdown / ICS / PDF export.
- `folders.py`, `patient_tasks.py`, `notifications.py`,
  `calendar_subscriptions.py` — folder CRUD + hardlinks, due-date
  tasks, outbound channels + opt-out, calendar subscriptions / ICS
  feeds.
- `annotations.py`, `tags.py`, `metadata_writes.py`,
  `clinical_notes.py`, `report_contents.py` — read + write
  annotations, tags (`tags:write`), study / series metadata
  (`studies:write_metadata`, `series:write_metadata`), clinical
  notes, report content versions.
- `summaries.py`, `qna.py`, `entities.py`, `labs.py`, `help.py` — LLM
  summaries, Q&A, document entities, lab timeseries, `bvphoenix help`
  introspection.
- `sharing.py` — mint and revoke study / folder share links
  (`Idempotency-Key` propagated).

The MCP layer is a strict superset of the GUI: every action the user
can take in the Health Record has at least one matching tool, plus a
few agent-only utilities (`get_my_scopes`, batch helpers). The
cross-patient invariant is enforced by the backend (composite FK +
per-request `enforce_agent_patient_scope`); there is no tool that
takes two `patient_id`s.

## Configuration for Claude Desktop (stdio)

```json
{
  "mcpServers": {
    "bitvision": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/bitvision_phoenix/mcp", "python", "-m", "bvmcp.server"],
      "env": {
        "BVP_MCP_BACKEND_BASE_URL": "http://localhost:8000",
        "BVP_MCP_USER_TOKEN": "<your-jwt-token>"
      }
    }
  }
}
```

## Configuration for Claude.ai (HTTP custom connector)

Create an AI assistant in Settings → AI assistants in the BitVision
UI; the page reveals `client_id` and `client_secret` exactly once.
Paste them, plus the MCP URL (`https://<mcp-host>/mcp`), into the
Claude.ai custom-connector dialog. See
`docs/agents-api/onboarding-mcp.md` for the full walkthrough.

## Architecture

```
MCP Client (Claude Desktop / Claude.ai / IDE / Agent)
    │
    │ stdio (JSON-RPC 2.0)        HTTP (JSON-RPC 2.0, Bearer)
    │                             │
    ▼                             ▼
bvmcp.server                    bvmcp.server_http
    │                             │  sha256(bearer) → /api/internal/agent-bearer/resolve
    │                             │  TTL cache (positive 60s, negative 10s)
    │ httpx + Bearer token        │  rate limit (per-token + per-IP)
    │                             │  forward Bearer to backend
    ▼                             ▼
            Backend REST API (FastAPI)
                       │
                       │ permissions (RLS + grants + per-assistant scope + patient set)
                       │
                       ▼
            PostgreSQL + pgvector + S3
```

See [docs/agent-protocols.md](../docs/agent-protocols.md) for the full
architecture including the A2A protocol and
[docs/agents-api/decisions/0019-remote-mcp-per-assistant-bearer.md](../docs/agents-api/decisions/0019-remote-mcp-per-assistant-bearer.md)
for the HTTP-transport ADR.
