# ADR 0010: Consultation finalize gating (scope dedicato non-agent)

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 4.5 introduce stati `draft, final, superseded` su
`Consultation` con transizione `draft -> final` via `POST .../finalize`
e check di completezza.

L'analisi della spec ha rilevato un gap di sicurezza/liability
critico: la spec non specifica chi può fare la transizione. Se un
agente LLM può finalizzare un consulto, il consulto diventa "approvato"
senza intervento umano. In contesto clinico, questo è inaccettabile:
un consulto finalizzato è un atto medico, deve avere un firmatario
umano.

Il codebase ha già `signed` come stato finale post-finalize, con
`signed_by_subject_id` e firma esplicita. Questo ADR si concentra sul
gate `draft -> final` (stato intermedio, non firma legale).

## Decision

**Scope dedicato `consultations:finalize`, NON concedibile a token
agent.**

Implementazione:

1. Aggiungere lo scope al catalogo (`auth/scopes.py` o equivalente).
2. Token issuance:
   - Token utente standard: può richiedere `consultations:finalize`
     se il subject ha la permission `WRITE_REPORT_FINALIZE` (o
     equivalente, da definire).
   - Token agent (`typ=agent` nel JWT): rifiuto della richiesta dello
     scope `consultations:finalize` a livello di issuer. Errore
     esplicito `agent_token_cannot_request_finalize_scope` (400).
3. Endpoint `POST /api/consultations/:id/finalize`:
   - Richiede scope `consultations:finalize`.
   - Verifica completezza: `summary_md`, `findings_md`,
     `recommendations_md` non null/empty; almeno una citation;
     status corrente == `draft`.
   - Setta `status=final`, `finalized_by_subject_id`, `finalized_at`.
   - Crea commit DAG di transizione.
4. PATCH consultation con `status=final` nel body: rifiutato.
   La transizione passa SOLO da `/finalize`.
5. MCP tool `finalize_consultation` esposto, ma il backend rigetta la
   chiamata se il token MCP è un agent token. Documentato in docstring.
   Se l'agente prova: 403 con `required_scope=consultations:finalize,
   agent_tokens_disallowed=true`.

## Consequences

### Positive

- Liability chiara: un consulto `final` ha un umano in loop documentato.
- Token revocation (vedi ADR 0016): se un agente è compromesso, non
  può finalizzare consulti dormant.
- Compliance: audit log della finalize ha sempre `subject_id` umano.

### Negative

- UX leggermente più macchinosa: agent prepara draft, umano clicca
  "finalizza".
- Edge case: esiste lo scenario "trusted agent" (non per ora) che si
  vorrebbe finalizzare in autonomia. Non supportato da questo ADR.
- Test setup più complesso: serve un secondo subject (umano) per
  validare la finalize.

## Alternatives considered

- **Permission RBAC senza scope**: meno granulare, lega l'autorità a
  ruoli statici. Scope-based è più componibile.
- **Trusted agent flag**: token con flag esplicito "puoi finalizzare".
  Apre superficie d'attacco, abbassa la garanzia di liability.
  Rifiutato.
- **Delay scheduled finalize ("agent prepara, after 24h diventa
  final")**: sostituisce umano con timeout. Fallisce ai requisiti di
  responsibility clinica.
- **Finalize via firma digitale obbligatoria**: troppo per la
  transizione draft -> final (che è uno stato intermedio). La firma
  digitale resta gate per `final -> signed` (stato esistente).

## Implementation hooks

- `auth/scopes.py` (Sprint 4): aggiunta scope.
- `auth/tokens.py`: rifiuto issuance per agent.
- `api/consultations.py`: endpoint POST finalize.
- `services/consultations.py`: helper `finalize(consultation_id,
  subject_id)` con check completezza.
- MCP `tools/consultations.py`: tool `finalize_consultation`.
- Test:
  - Token agent richiede scope `consultations:finalize` -> 400.
  - Token human con scope -> 200, status=final.
  - PATCH `status=final` via body -> 422.
  - Finalize su consultation incompleta -> 422 con lista campi
    mancanti.

## Note operative

- `final` non implica firma legale. La firma resta `signed_at +
  signed_by_subject_id`. UX deve esplicitare "finalize prepara per
  firma, signing è atto separato".
