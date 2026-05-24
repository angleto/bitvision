# Architectural Decision Records: Agents API

Indice degli ADR per l'estensione "Agents API". Ogni ADR cattura una
decisione di design con context, decision, consequences, alternatives.

Stati possibili:
- **Proposed**: bozza in discussione
- **Accepted**: decisione presa, vincolante
- **Superseded**: sostituito da ADR successivo (link)
- **Deprecated**: non più valido, vincolo storico

## Indice

| # | Titolo | Status |
|---|---|---|
| [0001](0001-document-versioning-dag.md) | Document versioning sul DAG git-like | Accepted |
| [0002](0002-idempotency-dryrun.md) | Idempotency-Key + dry_run interaction | Accepted |
| [0003](0003-bulk-replay-semantics.md) | Bulk replay semantics (atomic=false) | Accepted |
| [0004](0004-cross-patient-link.md) | Cross-patient invariant per Document-Study link | Accepted |
| [0005](0005-audit-aggregation.md) | Audit aggregation strategy (read vs write) | Accepted |
| [0006](0006-soft-delete-retention.md) | Soft-delete documenti + purge_after retention | Amended by 0020 |
| [0007](0007-ocr-pipeline.md) | OCR pipeline (Tesseract + pdfminer) | Accepted |
| [0008](0008-entity-confidence-schema.md) | Entity confidence schema (proposed vs validated) | Accepted |
| [0009](0009-citation-extension.md) | Citation file_id, page, bbox extension | Accepted |
| [0010](0010-consultation-finalize-gating.md) | Consultation finalize gating (scope dedicato non-agent) | Accepted |
| [0011](0011-dicom-tag-allowlist.md) | DICOM tag allowlist | Accepted |
| [0012](0012-mpr-cache-lru-disk.md) | MPR cache LRU disk | Accepted |
| [0013](0013-totalsegmentator-arm.md) | TotalSegmentator job offline (ARM compatibility) | Accepted (con spike pendente) |
| [0014](0014-long-ops-jobs-arq.md) | Long-running operations su pattern jobs Arq | Accepted |
| [0015](0015-openapi-snapshot.md) | OpenAPI snapshot first-class | Accepted |
| [0016](0016-token-revocation.md) | Token revocation | Accepted |
| [0017](0017-merge-file-ownership.md) | File ownership transfer al merge documenti | Accepted |
| [0018](0018-remote-mcp-oauth-authentik.md) | Remote MCP transport + OAuth 2.1 via Authentik | Superseded by 0019 |
| [0019](0019-remote-mcp-per-assistant-bearer.md) | Remote MCP transport with per-assistant bearer secrets | Accepted |
| [0020](0020-documents-hardlinks-and-no-orphan-invariant.md) | Document hardlinks, materialised patient root, no-orphan invariant, multi-referto | Accepted |

## Convention

- File naming: `NNNN-kebab-case-title.md` con N a 4 cifre.
- Numbering monotono crescente, niente buchi né riuso.
- Nuovi ADR superano i vecchi via campo "Superseded by" + status update sul vecchio.
- Sintesi (~1-2 pagine) preferita a documenti lunghi.
- Format minimo: Context / Decision / Consequences / Alternatives.
