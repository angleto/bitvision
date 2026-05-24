# Onboarding del connettore MCP remoto

> ADR di riferimento: [`0019-remote-mcp-per-assistant-bearer.md`](decisions/0019-remote-mcp-per-assistant-bearer.md).

Questo documento spiega come collegare bitvision phoenix a Claude.ai
(o ad altri host MCP che supportano i custom connector remoti) usando
le credenziali per-assistant emesse direttamente da phoenix.

## Modello di autenticazione in due righe

Ogni *AI assistant* configurato in phoenix possiede un proprio
`client_id` (formato `bvp_agt_<uuid>`) e un proprio `client_secret`
(generato server-side e mostrato all'operatore una sola volta). Il
client invia `Authorization: Bearer <client_secret>` a
`mcp.bitvision.example/mcp`; mcp-http hash-a il bearer e lo
risolve via RPC interna sul backend phoenix, ricostruendo la
`Principal` con scope e lista pazienti dell'assistant.

Niente OAuth, niente browser, niente refresh token. La revoca è
istantanea (toggle `is_active=false` in UI) o tramite rotate del
secret.

## Prerequisiti

- Un account utente attivo su `bitvision.example`.
- Browser moderno con cookie di sessione su Claude.ai.

## Flusso utente, passo per passo

1. Su `bitvision.example` apri **Settings → AI assistants** e
   clicca **+ New assistant**.
2. Compila il form:
   - **Name**: etichetta libera (es. "Claude in clinic").
   - **Provider** + **Model**: campi descrittivi per il tuo audit.
   - **Permissions**: spunta gli scope che vuoi concedere
     all'assistant (read documents/studies/consultations,
     write tags, write consultations, ecc.). Gli scope `danger`
     richiedono conferma esplicita perché toccano il record
     legale.
3. Conferma. La pagina mostra una **reveal-once card** con tre
   valori:
   - **MCP URL** — `https://mcp.bitvision.example/mcp`
   - **Client ID** — `bvp_agt_…`
   - **Client secret** — stringa lunga, mostrata una sola volta
   Copia subito i tre valori; il segreto in chiaro non è più
   recuperabile dopo la chiusura della card. Lato server è
   conservato solo `sha256(secret)`.
4. Su [https://claude.ai](https://claude.ai) vai in
   **Settings → Connectors → Custom connector** e inserisci:
   - **Server URL**: il valore *MCP URL*.
   - **OAuth Client ID**: il *Client ID*. (Nonostante il label,
     Claude.ai lo trasmette come bearer.)
   - **OAuth Client Secret**: il *Client secret*.
5. Conferma. Da quel momento, qualsiasi conversazione su Claude.ai
   vede i tool MCP listati (es. `list_patient_documents`,
   `get_fascicolo_bundle`). I tool che richiedono scope non
   concessi al tuo assistant ritornano `forbidden` con lo scope
   mancante in `detail`.

## Verifica rapida

In chat su Claude.ai:

```
Use list_patient_documents to show the most recent 5 documents
of patient X.
```

Se il tool risponde, il flusso è attivo.

## Pannello di gestione

In **Settings → AI assistants** ogni riga mostra `client_id`,
prefisso del secret (primi 8 caratteri), numero di pazienti
condivisi, stato attivo/revocato. Le azioni disponibili:

- **Edit** — rinominare, aggiornare provider/model, cambiare scope.
- **Rotate secret** — rigenera il `client_secret`. Mostra di nuovo
  la reveal-once card; il secret precedente smette di funzionare
  immediatamente (modulo la cache MCP, default 60s).
- **Revoke / Reactivate** — toggle `is_active`. Le richieste con
  il secret vengono rifiutate dal gate MCP finché non si
  riattiva.
- **Delete** — droppa l'assistant e tutti i grant pazienti
  associati.

## Limiti pratici

- **Rate limit**: 50 req/s per token, 200 req/s per IP (configurabili
  via `BVP_MCP_RATE_LIMIT_PER_TOKEN` / `BVP_MCP_RATE_LIMIT_PER_IP`
  sull'MCP HTTP container). Superare il cap restituisce 429 con
  Problem Details.
- **Cache di risoluzione**: positiva 60s, negativa 10s. Una rotate
  o revoca richiede al massimo `BVP_MCP_BEARER_CACHE_TTL_SECONDS`
  prima che il vecchio secret smetta del tutto di funzionare.
- **Scope mancanti**: tool che richiedono scope assenti dal token
  ritornano `forbidden` con lo scope mancante in `detail`.
- **PHI**: ogni risposta passa dal redaction filter del backend
  (`bvphoenix.logging.PHIRedactionFilter`). Lato MCP non vediamo PHI
  nei log, ma la conversazione su Claude.ai resta soggetta alla
  policy Anthropic — concorda con privacy officer se la condivisione
  PHI è autorizzata per i casi d'uso del tuo studio.

## Troubleshooting

| Sintomo | Causa probabile | Risoluzione |
| --- | --- | --- |
| `invalid bearer token` (401) sul gate MCP | Secret sbagliato, rotato, o assistant disattivato | Apri Settings → AI assistants, controlla stato. Rotate secret se non lo hai più. |
| `MCP auth misconfigured` (503) | `BVP_MCP_BACKEND_INTERNAL_KEY` vuoto sul pod mcp-http | Verifica il Secret `bvphoenix-internal` e il rollout. |
| `forbidden` con scope mancante nel `detail` | L'assistant non ha lo scope per quel tool | Edit assistant → spunta lo scope → Save. |
| `rate limit exceeded` (429 con `scope=token`) | Sessione MCP molto loquace | Attendi 1s o aumenta il cap via env (richiede rollout). |
| Secret valido ma "Couldn't reach the MCP server" su Claude.ai | DNS/TLS o ingress mcp non applicato | `kubectl -n bvphoenix-production get ingress` + `curl https://mcp.bitvision.example/health`. |

## Architettura sintetica

```
[Claude.ai]
   │  Authorization: Bearer <client_secret>
   ▼
[mcp.bitvision.example / mcp-http]
   • sha256(bearer) → POST /api/internal/agent-bearer/resolve
   • TTL cache (60s positivo, 10s negativo)
   • rate limit per token + per IP
   • forward Bearer al backend
       │
       ▼
[bitvision.example / phoenix backend]
   • lookup AgentAssistant by client_secret_hash
   • applica scope, patient grant, audit
```

## Riferimenti operativi

- Dashboard Grafana MCP: `grafana.bitvision.example/d/mcp-http`
  (latency, errori, rate-limit hit-rate). _Da creare in Sprint 2._
- Audit log: la tabella `audit_log` riceve un record per ogni call MCP
  HTTP con `action='mcp_http_request'`. Vedi
  [`docs/security-audit-log.md`](../security-audit-log.md) per le
  convenzioni di tag.
- Controlli post-deploy: `kubectl -n bvphoenix-production rollout
  status deploy/bvphoenix-mcp-http` + `curl
  https://mcp.bitvision.example/health`.
