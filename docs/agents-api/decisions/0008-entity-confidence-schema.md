# ADR 0008: Entity confidence schema (proposed vs validated)

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 4.3 propone `extract_document_entities` con codici clinici
(ICD-10, LOINC, SNOMED-CT) marcati come "best-effort" via suffisso `?`
sul codice (`"icd10cm": "C25.0?"`).

L'analisi della spec ha identificato due problemi:

1. **Suffisso nel codice**: anti-pattern. Codice non parsabile
   programmaticamente da downstream sistemi (ETL, search, integrazioni
   FHIR future). Forza string-stripping ovunque.
2. **Uso clinico**: spec dice "MAI usati clinicamente" ma senza
   separazione strutturale, qualcuno finirà per usarli. Hope is not a
   strategy.

## Decision

**Confidence come campo separato + namespace separato proposed/validated**.

Schema:

```json
{
  "entities_proposed": {
    "procedures": [
      {
        "text": "RM addome superiore con MdC",
        "start": 0, "end": 28, "page": 1,
        "icd10cm": "C25.0",
        "confidence": 0.42,
        "extractor": "rules-it-v0.3",
        "validation_status": "unverified"
      }
    ],
    "lab_values": [...],
    ...
  },
  "entities_validated": {
    "procedures": [
      {
        "text": "...",
        "icd10cm": "C25.0",
        "confidence": 1.0,
        "extractor": "rules-it-v0.3",
        "validation_status": "validated",
        "validated_by_subject_id": "...",
        "validated_at": "..."
      }
    ]
  },
  "extractor_version": "v0.3-rules-it",
  "extracted_at": "..."
}
```

Stati `validation_status`:

- `unverified`: estratto automaticamente, mai rivisto.
- `reviewed`: esaminato da clinico, lasciato in `entities_proposed`
  (es. clinico ha guardato ma non ha applicato).
- `validated`: clinico ha confermato, codice promosso in
  `entities_validated`.

Codici clinici sempre stringa pulita, niente suffisso. `confidence` è
float `[0, 1]`. Per estrattori rule-based la confidence è una stima
calibrata; per modelli BERT clinici futuri sarà la posterior probability
calibrata.

Endpoint API:

- `GET .../entities` ritorna entrambi i namespace.
- `POST .../entities/:entity_id/validate` (Sprint 4 stretch) sposta
  un'entity da proposed a validated. Scope `entities:validate`.
- Search index include solo `entities_validated`.
- Default per agent: vede entrambi, ma viene istruito (system prompt
  MCP) che proposed è non clinico.

## Consequences

### Positive

- Codici clinici machine-readable, ready per FHIR export futuro.
- Validation status esplicita: nessuna confusione su cosa è clinicamente
  approvato.
- Confidence come float unifica rule-based e ML.
- Search e analytics possono filtrare deterministicamente
  (`entities_validated` per dashboard cliniche, entrambi per ricerca
  esplorativa).

### Negative

- Schema più verboso (2 namespace + status field) rispetto a una lista
  semplice.
- UI deve distinguere visivamente proposed vs validated (badge, colore).
- Migration: entities legacy esistenti (se presenti) vanno spostate
  tutte in `entities_proposed` con `confidence=null,
  validation_status=unverified`.

## Alternatives considered

- **Suffisso `?` nel codice** (spec originale): rifiutato, vedi context.
- **Solo confidence, senza namespace**: chiunque può filtrare per
  `confidence > 0.95` ma il rischio "uso clinico di unverified" rimane.
  Namespace è guardrail strutturale.
- **Validation status su un'entity unica**: meno duplicato ma rende
  filtering più complesso e non comunica chiaramente l'intento.

## Implementation hooks

- `services/clinical_entities.py` (Sprint 4): orchestratore.
- Schema persistito in `patient_documents.entities_jsonb` (campo
  unico JSONB con i due namespace).
- Indice GIN su `entities_validated` per search future.
- Test:
  - Estrazione idempotente: stesso text + stesso version → byte-equal.
  - Validation: POST validate sposta da proposed a validated.
  - Search filter: `entities_validated` only.

## Open questions

- Set base di codici (ICD-10-CM, LOINC, SNOMED-CT) e relativa licenza
  d'uso? SNOMED-CT richiede membership. Alternativa: codifiche italiane
  (ICD-9-CM ministeriale) per partenza, SNOMED quando licenza.
