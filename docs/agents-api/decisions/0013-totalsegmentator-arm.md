# ADR 0013: TotalSegmentator job offline (ARM compatibility)

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 5.5 propone integrazione di TotalSegmentator
(https://github.com/wasserth/TotalSegmentator), modello open-source
nnU-Net che segmenta 100+ strutture anatomiche su CT. Use case:
volumetria automatica (fegato, milza), centroidi, max diameter, density
media.

Vincoli architetturali rilevati durante l'analisi:

- Production cluster: managed Kubernetes, 2 **ARM64** nodes.
- TotalSegmentator dipende da PyTorch + nnU-Net + nibabel + simpleitk.
  Storicamente, le wheel pre-built per Linux ARM64 di queste deps sono
  irregolari. PyTorch 2.x ha wheel ARM linux ma nnU-Net e
  TotalSegmentator stesso non distribuiscono ARM build ufficiali.
- Build from source su ARM richiede compilazione (ore in CI), risultato
  non sempre identico a x86 (numerical determinism, BLAS variants).
- GPU non disponibile in cluster (CPU-only deploy). TotalSegmentator
  CPU mode esiste ma è 3-5x più lento.

## Decision

**Verifica ARM compatibility PRIMA di committere su Sprint 6, non
dopo.**

Sprint plan:

1. **Spike validation (Sprint 5 stretch o Sprint 6 settimana 1)**:
   - Build immagine workers con TotalSegmentator + deps su ARM Mac
     (nativamente ARM64) e su build ARM64 in cluster CI.
   - Test smoke: caricare un volume CT phantom, eseguire
     `total_segmentator(input_path, output_path, fast=True)`,
     misurare tempo + correttezza output.
   - Risultato: documento "ARM compat report" che decide se
     procedere o se ripiegare su alternative.

2. **Se compatibile**:
   - Job offline `workers/src/bvworkers/tasks/totalsegmentator.py`,
     dispatched a ingest CT (filtrando modality == CT). Skip per RX,
     RM, US, ecc.
   - Output salvato come NIfTI in S3 bucket dedicato
     (`segmentations/<series_id>/<structure>.nii.gz`).
   - Tabella `series_segmentations` (vedi spec 5.5):
     `series_id, structure, model_version, volume_ml, centroid_mm,
     mask_url`.
   - Endpoint `GET /api/series/:sid/segmentation-records` ritorna lista
     (path distinto da `/segmentations` perché quest'ultimo serve già la
     legacy S3 listing dei `.bin` mask).

3. **Se non compatibile**:
   - Fallback A: deploy nodo x86 dedicato per workers GPU/segmentation
     (costo extra Scaleway, capacity planning richiesto).
   - Fallback B: API esterna (es. self-host TotalSegmentator gRPC su
     server x86 fuori cluster). Aggiunge latency e single point of
     failure.
   - Fallback C: sostituire con MONAI Label o nnU-Net stand-alone con
     wheel ARM disponibile. Coverage di strutture diverso, valutare.
   - Fallback D: posporre Phase 3 segmentation a infinito.
     Accettabile, non blocca MVP.

Decisione su quale fallback prendere: **rimandata al risultato dello
spike**, non ora.

## Consequences

### Positive

- Risparmio di tempo: niente sviluppo Sprint 6 prematuro su feature
  che potrebbe non essere deployable.
- ADR documenta esplicitamente l'incertezza, futuro lettore non parte
  da false premesse.

### Negative

- Sprint 6 ha alea: lo spike va fatto bene per essere informativo. Una
  giornata di setup ARM build è realistica.
- Se finiamo su fallback B (API esterna), vincolo architetturale a
  server x86 separato, opex addizionale.

## Alternatives considered

- **Skip ARM check, partire e poi vedere**: rifiutato. Scoprire al
  deploy che TotalSegmentator non gira è caro.
- **Cluster x86 invece di ARM**: già deciso (cost optimization:
  managed K8s, 2 nodi ARM64). Cambio decisione fuori scope di questo
  ADR.
- **Saltare TotalSegmentator del tutto**: ammissibile per MVP, riduce
  Phase 3 scope ma mantiene Sprint 5 imaging base. Decisione formale
  rimandata a post-spike.

## Implementation hooks

- Spike Sprint 6 settimana 1:
  - Branch `spike/totalsegmentator-arm-validation`.
  - Dockerfile ARM con TotalSegmentator install.
  - Smoke test: phantom CT, output NIfTI, diff vs ground truth.
  - Report markdown in `docs/agents-api/spikes/totalsegmentator-arm.md`.
- Decisione formale post-spike: superseded ADR (0013-rev2) con
  scelta tra fallback A/B/C/D.

## Note operative

- Anche se compatibile, valutare se "ingest-time segmentation" è il
  trigger giusto, oppure "on-demand quando l'agente la chiede". L'on-demand
  riduce storage di NIfTI inutilizzati ma allunga la prima chiamata di
  diversi minuti.
- Privacy: segmentazioni sono derivate da PHI ma non identificative
  per sé stesse. Possono vivere in bucket separato (con stesso
  retention).
