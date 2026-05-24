# ADR 0017: File reference counting su S3 al merge

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 3.4 introduce `merge_documents`:

```
POST /api/patients/:pid/documents/:primary_id/merge
{
  "duplicate_ids": ["..."],
  "merge_strategy": "keep_primary",
  "preserve_files_as_attachments": true,
  "reason": "..."
}
```

Il flag `preserve_files_as_attachments: true` aggancia i binari dei
duplicati come `files[]` del primary invece di rimuoverli.

L'analisi della spec ha identificato un edge case:

- Duplicato D1 ha file F1 in `s3://bucket/key1`.
- Merge primary P1 con D1, `preserve_files_as_attachments=true`.
- Risultato proposto: F1 referenziato sia da P1.files (nuovo) sia da
  D1.files (esistente, soft-deleted).
- Quando D1 viene hard-deleted (purge_after), cosa succede a
  `s3://bucket/key1`?
  - Se eliminato: rompe P1 (file referenziato).
  - Se non eliminato: garbage S3 immortale.
- Reference counting su S3 keys è soggetto a race nel job di purge.

## Decision

**Ownership transfer al merge: i file del duplicato passano sotto P1,
spariscono dal duplicato.**

Implementazione:

1. Pre-merge: D1.files = [F1], P1.files = [F0].
2. Post-merge:
   - P1.files = [F0, F1] (entrambi i file ora ownership di P1).
   - D1.files = [] (vuoto, soft-deleted).
3. Hard-delete D1 (purge_after): D1 row eliminata, ma F1 NON viene
   toccato perché non più referenziato da D1.
4. Hard-delete P1 (purge_after, se ce ne fosse): F1 viene eliminato
   insieme a F0.

Reference counting non serve: l'invariante è "ogni file ha esattamente
un document owner". Il merge sposta ownership, niente sharing.

Migration: niente migration. Implementazione applicata solo alle nuove
merge da Sprint 3.

## Consequences

### Positive

- Niente reference counting su S3: semplice, niente race.
- Job di purge (sia documents sia jobs) può eliminare file S3 senza
  paura di breaking link.
- Audit log della merge esplicita "file F1 trasferito da D1 a P1".

### Negative

- Se per qualche ragione l'utente fa "restore di D1" (vedi ADR 0006),
  D1 viene ripristinato ma i suoi file sono ora di P1. UX edge case:
  dopo restore, D1 ha `files = []`. Documentare nell'help.
- Audit log della merge deve essere chiaro su transfer ownership per
  fini forensics. La traccia "F1 era di D1" viene preservata in
  audit ma non in tabella corrente.

### Mitigazioni

- Restore di un documento merged: messaggio UI esplicito "i file di
  questo documento sono stati trasferiti al documento <P1> il
  <date>. Per ripristinare lo stato pre-merge, contatta admin". Niente
  auto-rollback (rischio di rompere P1).
- Audit log merge salva snapshot `files_transferred: [{file_id,
  from_document_id, to_document_id}]`.

## Alternatives considered

- **Reference counting esplicito su `s3_files` table**: tabella nuova
  con `s3_key, ref_count, owners[]`. Job di purge decrementa ref_count,
  elimina S3 quando 0. Più complesso, race condition possibili (due
  document hard-delete simultanei sullo stesso file).
- **Lazy garbage collection**: job notturno scansiona S3, identifica
  key non referenziate da nessun `PatientDocumentFile`, le elimina.
  Funziona ma costoso (full scan), latency elevata per spazio
  liberato.
- **Copia il file invece di trasferire**: D1.files = [F1] (originale),
  P1.files = [F0, F1_copy] (nuovo S3 key). Doppio storage, niente
  ambiguità. Costo storage non accettabile per file grandi (PDF
  100MB+).
- **Disabilitare `preserve_files_as_attachments`**: semplifica ma
  rimuove la feature di "preservare i binari del duplicato per
  audit". Rifiutato.

## Implementation hooks

- `services/documents.py` (Sprint 3): nuova funzione
  `merge_documents(primary_id, duplicate_ids, preserve_files,
  reason, actor)` con transazione SQL che:
  1. Per ogni duplicato D in `duplicate_ids`:
     - Se `preserve_files_as_attachments=true`:
       - Per ogni file F in D.files: setta `F.document_id = primary_id`.
     - Altrimenti: i file di D vengono lasciati orfani (saranno purgati).
  2. Soft-delete D (vedi ADR 0006).
  3. Audit log entry "documents.merged" con snapshot transfer.
- Constraint DB: `PatientDocumentFile.document_id` ha FK a
  `PatientDocument.id` (esistente). Niente cambio schema.
- Test:
  - Merge di 2 doc, primary mantiene 1 file iniziale + 1 trasferito.
  - Hard-delete duplicato: file rimane (ora ownership primary).
  - Hard-delete primary post-merge: tutti i file rimossi.
  - Restore duplicato post-merge: D ha files=[].
  - Audit log contiene snapshot files_transferred.

## Note operative

- Storage S3: bucket policy lifecycle (vedi infra) gestisce eviction
  finale di file S3 orfani non referenziati per > N giorni.
- Per `preserve_files_as_attachments=false` (rare): file del
  duplicato vengono soft-orphan e purgati a 30 giorni dal job di
  cleanup S3.
