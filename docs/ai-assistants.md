# AI assistants

Configuration model for the LLM agents (Claude Desktop, GPT, Ollama,
…) that talk to bitvision phoenix via MCP. Replaces the earlier
per-patient agent-token wizard.

## Why a per-user model

A medico typically uses **one or more** AI assistants and shares **one
or more** patients with each. The same patient can also be shared
with multiple assistants for benchmark / second-opinion workflows.
Modelling tokens as `(user, patient)` pairs (the legacy design) made
that workflow clumsy: rotating a token meant re-typing labels and
permissions, and a patient shared with two assistants needed two
unrelated tokens.

The current model splits identity, credential, and access:

| Table | Purpose | Cardinality |
|---|---|---|
| `agent_assistants` | identity (label, provider, model_id, permissions) | one per assistant per user |
| `agent_tokens` | bearer credential | one *active* per assistant; rotated rows kept for audit |
| `agent_assistant_patients` | which patients each assistant may see | N–N |

`backend/src/bvphoenix/db/models/agent_tokens.py` carries all three
SQLAlchemy classes.

## API

Mounted at `/api/ai-assistants` (see
`backend/src/bvphoenix/api/ai_assistants.py`):

| Method | Path | Effect |
|---|---|---|
| POST   | `/api/ai-assistants` | create assistant + mint first token (raw JWT in response, exposed *exactly once*) |
| GET    | `/api/ai-assistants` | list user's assistants with `active_token` summary + patient count |
| GET    | `/api/ai-assistants/{id}` | detail |
| PATCH  | `/api/ai-assistants/{id}` | edit label / provider / model_id / notes / permissions |
| DELETE | `/api/ai-assistants/{id}` | cascade delete (token + patient links) |
| POST   | `/api/ai-assistants/{id}/rotate` | revoke current, mint new, return raw token |
| GET    | `/api/ai-assistants/{id}/patients` | list shared patients |
| POST   | `/api/ai-assistants/{id}/patients` | share a patient (`{patient_id}`) |
| DELETE | `/api/ai-assistants/{id}/patients/{patient_id}` | un-share |

The permission whitelist is narrow on purpose:
`patient:read`, `patient:images`, `consultation:read`,
`consultation:write`. Unknown verbs are 400'd before any DB write.

## Auth flow at runtime

When a request carries an agent JWT (`auth/deps.py::_resolve_user`):

1. SHA-256(raw JWT) → `AgentToken` row;
2. row not revoked + not expired;
3. the assistant's allowed patient set is loaded *once per request*
   from `agent_assistant_patients` and cached on
   `request.state.agent_patient_ids`;
4. `enforce_agent_patient_scope(request, patient_id)` checks set
   membership in O(1) — no DB round-trip per chokepoint.

`require_agent_scope("consultation:write")` and similar continue to
work; they read `request.state.agent_scope` which is populated from
`AgentAssistant.permissions` at mint time.

## Frontend

- `/settings/ai-assistants` — list / create / rotate / delete
  assistants; expand a row to manage its patient list.
- "Share with AI" button on the Health Record opens
  `ShareWithAiModal`: a checkbox per assistant. The same patient can
  be shared with multiple assistants from this dialog.
- `McpConfigSnippet` produces the `claude_desktop_config.json`
  snippet (server name derived from the assistant label) on
  create/rotate.

## Migration history

- `0041_ai_assistants` — created the three tables, repointed
  `agent_tokens.assistant_id`, dropped `patient_id`/`label`/
  `permissions` from `agent_tokens`. Existing rows in `agent_tokens`
  were dropped (dev-only restructure).
