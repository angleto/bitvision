# Claude Desktop quickstart: 5 minutes to a live fascicolo

Connect Claude Desktop to your bitvision phoenix Health Record in
five steps. When done, you can ask Claude to "analyze patient X's
record" and get answers based on real data (DICOM studies, reports,
clinical documents, annotations), with images when available.

Estimated time: 5 minutes. No deploy required. Everything local or
against a remote backend of your choice.

## Prerequisites

1. **Claude Desktop** installed: [download from claude.ai](https://claude.ai/download).
2. **bitvision phoenix** reachable:
   - local: `make up.infra && make db.migrate && make backend.dev`
     (default `http://localhost:8000`), or
   - remote: the URL of the production backend you have access to
     (e.g. `https://app.bit.vision`).
3. **Python + uv** (to run the MCP server):
   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
4. A **bitvision user** with at least one visible patient (grant or
   ownership). See [fascicolo.md](./fascicolo.md) to populate.

## Step 1: prepare the MCP module

From the repo root:

```sh
cd mcp
uv sync
```

`uv sync` installs the MCP server (`bvmcp.server`) along with `httpx` and
the official `mcp` SDK. You do not need to start it manually: Claude Desktop
will launch it on stdio every time you open the app.

Optional smoke test:

```sh
uv run python -m bvmcp.server --help 2>/dev/null || echo "ok: modulo installato"
```

## Step 2: create an AI assistant

AI assistants are per-user identities with their own permissions and
their own patient share list. Each assistant carries at most one
active credential and is revocable independently.

For Claude Desktop (stdio transport) the bearer is a regular
user-scoped JWT, scoped down by the assistant's permissions and
patient list. For Claude.ai (HTTP custom connector) the credential
is a per-assistant `client_id` + `client_secret` (ADR 0019); see
[`agents-api/onboarding-mcp.md`](./agents-api/onboarding-mcp.md).

1. Open bitvision in the browser and log in.
2. Open **Settings → AI assistants** (`/settings/ai-assistants`).
3. Click **+ New assistant**.
4. On the form:
   - **Label**: free text ("Claude Desktop, work laptop").
   - **Provider / model**: descriptive fields for your own audit.
   - **Permissions**: tick the scopes you want to grant (read +
     optional write families). `danger`-marked scopes ask for
     explicit confirmation because they touch the legal record.
   - **De-identify on use**: `ON` to hide name, tax code, exact
     date of birth, address from the assistant. Clinical metadata
     (modality, approximate exam date, reports) remain visible.
   - **TTL** (stdio only): 1h / 24h / 7 days / custom.
5. Confirm. The page surfaces a **reveal-once card** with the bearer
   value(s). Copy them now; they are unrecoverable afterwards.
6. Open the **Share with AI** button on the patient's Health Record
   to make a specific patient visible to this assistant. Same patient
   can be shared with multiple assistants.

> **Local backend without the UI yet?** You can use a regular login
> JWT for the stdio transport while you set up the first assistant:
> ```sh
> export JWT=$(curl -s -X POST http://localhost:8000/api/auth/login \
>   -H 'Content-Type: application/json' \
>   -d '{"email":"you@example.com","password":"…"}' \
>   | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
> echo "$JWT"
> ```
> The same token works as `BVP_MCP_USER_TOKEN` (MCP delegates to the
> backend, which applies RLS and grants).

## Step 3: paste the snippet into Claude Desktop

Open the Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

If it does not exist, create it. Paste (replacing the placeholders):

```json
{
  "mcpServers": {
    "bitvision": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/absolute/path/to/bitvision_phoenix/mcp",
        "python",
        "-m",
        "bvmcp.server"
      ],
      "env": {
        "BVP_MCP_BACKEND_BASE_URL": "http://localhost:8000",
        "BVP_MCP_USER_TOKEN": "PASTE-YOUR-AGENT-TOKEN-HERE"
      }
    }
  }
}
```

Notes:

- `--project` **must** be absolute (Claude Desktop does not expand
  `~`).
- For a remote backend, change `BVP_MCP_BACKEND_BASE_URL` to
  `https://app.bit.vision` (or your host).
- If you have multiple backends, you can declare multiple MCP servers
  (`bitvision-prod`, `bitvision-staging`) with distinct names.

## Step 4: restart Claude Desktop and verify

1. **Full quit** of Claude Desktop (Cmd/Ctrl-Q: the X at the top is not
   always enough).
2. Reopen.
3. In a new conversation, click the **"Search and tools"** icon
   (gear / wrench at the bottom left of the textbox). Search for **"bitvision"**.
4. `bitvision` should appear with the tools that match the scopes
   granted to your assistant — search, studies, series, patients,
   fascicolo bundle, care phases, documents, segmentations, sharing,
   and so on. The live catalogue is the directory listing of
   `mcp/src/bvmcp/tools/`; the per-scope inventory is in
   [`agent-protocols.md`](./agent-protocols.md).

If it does not appear, jump to **Troubleshooting**.

## Step 5: try it end-to-end

In chat with Claude:

```
Analizza il fascicolo del paziente 8b2a34de-4e0e-4c0f-9d1b-3f0a1b2c3d4e.
Mostrami la timeline cronologica, riassumi i referti e identifica
eventuali follow-up suggeriti.
```

Claude will call (typically, in sequence):

1. `get_patient`: demographics
2. `get_fascicolo_index`: counts per section
3. `get_patient_timeline`: ordered events
4. `list_reports` on individual studies: report text
5. optionally `get_series_thumbnail`: inline image in the thread

Claude Desktop shows each tool call as an expandable card; you have
veto rights on each one (click "Allow" / "Deny").

Other example prompts:

```
Trova studi simili al CT toracico <study-id> e confronta i referti.
```
```
Mostrami tutti i documenti clinici del paziente <id> caricati
nell'ultimo anno.
```
```
Sulla serie <series-id> descrivimi cosa vedi e salva un'annotazione.
```

## Troubleshooting

### "bitvision" does not appear among the MCP servers

- Check the JSON syntax (commas, double quotes). Claude
  Desktop **stays silent** if the file does not parse.
- Check the logs:
  - macOS: `~/Library/Logs/Claude/mcp*.log`
  - Windows: `%APPDATA%\Claude\logs\mcp*.log`
- Run the command manually to see the actual error:
  ```sh
  uv run --project /abs/path/mcp python -m bvmcp.server
  ```
  It should print `MCP server started on stdio` and stay waiting
  (Ctrl-C to exit).

### `401 Unauthorized` / "token expired"

- The JWT has expired: go back to `/settings/ai-assistants`,
  **Rotate secret** (or recreate the assistant), update
  `BVP_MCP_USER_TOKEN` with the new bearer, **Quit+Restart**
  Claude Desktop.
- If you regenerated the token without restarting, MCP is still using
  the old one: restart is mandatory.

### `Connection refused` / `ECONNREFUSED 127.0.0.1:8000`

- The backend is not running. From a separate terminal:
  `make backend.dev`.
- If you use Docker, check the port mapping
  (`docker ps | grep 8000`).
- Remote backend: check that your IP is not blocked and that the
  domain resolves (`curl -I https://app.bit.vision/.well-known/agent-card.json`).

### Claude does not call any tool

- Rephrase the prompt with explicit imperative verbs ("search",
  "fetch", "show"). Claude sometimes responds verbally without a tool
  call if the prompt seems rhetorical.
- In the "Search and tools" panel, check that `bitvision` is
  **enabled** (green toggle).
- Ask a direct question that requires data: "How many studies does
  patient <id> have?". This forces `get_fascicolo_index`.

### Images do not appear in the thread

- `get_series_thumbnail` and `get_study_thumbnails` are live (see
  [agent-protocols.md](./agent-protocols.md)). If a series has no
  pixel data (e.g. structured report), the backend responds `422`;
  Claude reports this clearly in the thread. As an alternative, ask
  Claude for a clickable link: `"genera un link allo studio per il
  viewer"`. You will get `/studies/<id>` to open in the browser.

### `describe_series` / `embed_series` fail

- `describe_series` uses Anthropic via the backend: it needs
  `BVP_ANTHROPIC_API_KEY` in the backend `.env`, not on the MCP side.
- `embed_series` requires workers with the `ai` extra:
  `cd workers && uv sync --extra ai && make workers.dev`.

## Security: what Claude sees, what it does not

### What Claude **sees** (token scope)

- Patients, studies, series, reports, documents, annotations to which the
  **subject who owns the token has access** (grants + ownership).
- With `scope=read-fascicolo` (default): reads only.
- With `scope=read+write`: it can also create consultations, annotations,
  LLM descriptions.

### What Claude **does not** see

- Data of other patients not shared with you (RLS and grants
  filter before the HTTP response).
- If **De-identify = ON**: first name, last name, exact date of birth,
  tax code, address, contacts are **removed or generalized**
  (age by decade, initials only). Clinical metadata and pixel-anonymized
  images (DICOM PixelData with burned-in PHI removed by the
  worker) pass through unchanged.
- System secrets (JWT secret, S3 keys, password hashes).
- Other agent tokens: Claude cannot enumerate or generate new
  tokens.

### Audit

Each tool call is an HTTP call to the backend with your bearer token;
a row is written in `audit_log` with `principal=<you>`,
`user_agent=mcp:bvmcp/<version>`, `tool=<name>`. Review it in
`/settings/audit` or via `GET /api/audit`.

### Revocation

`/settings/ai-assistants` → assistant row → **Rotate secret** (new
credential, the old one stops working within the MCP HTTP cache TTL,
default 60s) or **Revoke** (`is_active=false`, every future request
is rejected at the auth gate). The auth resolver enforces both
`is_active=false` and a non-null `revoked_at` (3.7.9 H2: single-flag
bypass is closed by the migration that added `revoked_at`).

## Next steps

- Read [agent-protocols.md](./agent-protocols.md) for the complete
  architecture (MCP + A2A + REST).
- For a **headless** agent (without Claude Desktop) that orchestrates
  multimodal end-to-end: [`examples/fascicolo_consultation/`](../examples/fascicolo_consultation/).
