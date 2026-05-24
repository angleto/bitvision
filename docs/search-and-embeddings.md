# Search & Embeddings Guide

Complete guide to search in bit.vision: from tag-first queries to
hybrid semantic fusion, covering the embedding model registry and
HNSW index tuning.

Feature status: **partially landed**. What is currently in code
is marked *(landed)*; what is described in the v3 plan but not yet
merged is marked ***(planned)***.

---

## 1. Philosophy: tag-first, semantic complementary

Search in bit.vision follows a **deterministic cascade** in 4
stages, from most precise to most elastic. The idea is that most
real clinical queries (e.g. "chest CT 2023", "patient X record",
"lung segments") are better served by structured filters and
curated tags than by pure vector similarity: semantics is a
*complement*, not the primary engine.

```
user query
    │
    ▼
┌────────────────────────┐
│ 1. Tag filter          │  exact ns:value, B-tree indexed
│    anatomy:lung,       │  (structural filters, ~1ms cost)
│    protocol:CT-chest   │
└────────────┬───────────┘
             │ reduces the candidate set
             ▼
┌────────────────────────┐
│ 2. Full-text (tsvector)│  GIN index on study_description +
│    "contrast enhancement"│ series_description (migration 0002)
└────────────┬───────────┘
             │
             ▼
┌────────────────────────┐
│ 3. Semantic (pgvector) │  BiomedCLIP text→image (512-dim)
│    "lung nodule 8mm"   │  HNSW cosine, tunable ef_search
└────────────┬───────────┘
             │
             ▼
┌────────────────────────┐
│ 4. Hybrid (RRF)        │  Reciprocal Rank Fusion between 2) + 3)
│    combined ranking    │  + optional 1) as pre-filter
└────────────────────────┘
```

**Why tag-first reduces complexity.** A tag `anatomy:lung`
narrows the set to hundreds of studies at constant cost, before
running any expensive operator (tsvector rewrite, HNSW traversal).
Tags are also *editable* by the user: if semantic segmentation
quality degrades, the radiologist operator regains control by
tagging manually.

The `Tag` data model lives in
`backend/src/bvphoenix/db/models/annotations.py:99` and supports
`target_kind ∈ {study, series, instance, dataset}` with
`UNIQUE(target_kind, target_id, namespace, value)`: the same tag
cannot be duplicated on the same target.

---

## 2. 4-layer architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ Layer 1, Ingest                                                      │
│   DICOM upload → frontend/src/app/upload/*                           │
│   Worker pack_volume (workers/src/bvworkers/tasks/pack_volume.py)    │
│   Worker embed_series (workers/src/bvworkers/tasks/embed_series.py)  │
│     → BiomedCLIP visual encoder (middle slice, 512-dim)              │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Layer 2, Storage                                                     │
│   Postgres                                                           │
│     ├─ studies / series / instances        (metadata)                │
│     ├─ tags (ns:value)                     (B-tree)                  │
│     ├─ tsvector GIN idx on descriptions    (migration 0002)          │
│     ├─ embeddings (pgvector, dim=512)      (HNSW cosine)             │
│     └─ text_embeddings (dim=384)           (planned)                 │
│   S3 / R2: original DICOM + derivatives (packed volume)              │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Layer 3, Search API                                                  │
│   /api/search                (landed; tag + ts + sort + facets)      │
│   /api/similar-to/{id}       (landed; image→image BiomedCLIP)        │
│   /api/search/semantic       (planned; text→anything BiomedCLIP)     │
│   /api/search/hybrid         (planned; RRF 3-way)                    │
│   /api/patients/{id}/search  (planned; patient-scoped hybrid)        │
│   /api/tags*                 (planned; CRUD + tree + autocomplete)   │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Layer 4, UI / Agents                                                 │
│   Viewer SimilarCasesPanelViewer.tsx  → /api/similar-to              │
│   Page frontend/src/app/search/visual (planned; semantic panel)      │
│   A2A skill "similarity-search"       → /api/similar-to              │
│   MCP tool bvphoenix.search                                          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Embedding models

### 3.1 BiomedCLIP (512-dim, cross-modal) *(landed)*

- Model:
  `microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224`.
- Visual encoder: per series, middle-slice,
  `workers/src/bvworkers/tasks/embed_series.py`.
- Text encoder: per report, annotation, consultation, document
  (***planned***: at the moment only the image encoder is invoked).
- Single table `embeddings`
  (`backend/src/bvphoenix/db/models/embeddings.py`), `dim=512`.
- Shared text/image vector space: a text query
  ("lung nodule ground-glass") can directly match series
  embedded via the image encoder ("text-to-image" search).
- Column `model_id = 'biomedclip-v1'`; uniqueness
  `(target_kind, target_id, model_id)`.

### 3.2 Multilingual sentence-transformers (384-dim, text-to-text) ***(planned)***

- Planned model: `intfloat/multilingual-e5-small` or
  `paraphrase-multilingual-MiniLM-L12-v2`.
- Dedicated table `text_embeddings` (separate pgvector, `dim=384`).
- Rationale: the BiomedCLIP text encoder is optimized on English
  PubMed abstracts; on mixed it+en reports (typical of Italian
  Health Records) a multilingual sentence-transformer gives
  superior text-to-text quality.
- Targets: report, consultations, patient_documents, annotations of
  type `description`.
- pgvector requires fixed dimension per column: two separate tables
  (no union vectors).

### 3.3 Model Registry ***(planned)***

- Table `embedding_models` with fields
  `model_id, dim, modality, target_kind, vector_table, added_at`.
- CLI: `bvphoenix-embed-models list|add|deprecate` (TBD in
  `backend/src/bvphoenix/cli/`).
- Rationale: add a new model (e.g. upgrade
  `biomedclip-v1 → biomedclip-v2`) *without code change* on the backend:
  the registry routes queries to the correct vector table based on
  `model_id`.
- See `docs/model-registry.md` ***(planned)***.

---

## 4. Search endpoints

Auth note: `optional_user` applies `visible_studies_filter`
(`backend/src/bvphoenix/services/permissions.py`), so results
are always restricted to what the caller can see.

### 4.1 `GET /api/search` *(landed, tag + ts + filters)*

Source: `backend/src/bvphoenix/api/search.py:32`.

Query params:
- `q` (max 128 chars): full-text on `study_description` +
  `series_description`, via `plainto_tsquery('simple', ...)`.
- `modality`: array match on `Study.modalities`.
- `body_part`: ILIKE on `Series.body_part_examined`.
- `date_from`, `date_to`: range on `Study.study_date`.
- `tag`: repeatable, format `namespace:value`
  (e.g. `tag=anatomy:lung&tag=protocol:CT-chest`).
- `limit` (1-200, default 50), `offset`.

Response: `PaginatedStudies` (`{items, total, limit, offset}`).

**Sort + facets** ***(planned S1)***: addition of `sort`
(`relevance|date_desc|date_asc|modality`) and a `facets` block in
the response with counts per `modality`, `body_part`, `tags`.

Example:

```bash
curl "http://localhost:8000/api/search?q=ground-glass&tag=anatomy:lung&modality=CT&limit=20"
```

### 4.2 `GET /api/search/semantic` ***(planned S3)***

Text-to-anything via BiomedCLIP text encoder + pgvector.

Expected shape:

```
GET /api/search/semantic?q=<string>&target_kind=series|report|...
                       &k=10&modality=CT
```

Response: `[{target_kind, target_id, score, snippet?}]`.

Includes a **query embedding cache** (section 6) to deduplicate
repeated queries.

### 4.3 `GET /api/search/hybrid` ***(planned S2)***

3-way fusion with Reciprocal Rank Fusion (section 5) of:
1. tsvector ranking (`ts_rank`);
2. BiomedCLIP text→image ranking;
3. sentence-transformer text→text ranking (if available for the
   `target_kind`).

Query params: same as `/api/search` + optional `weights=<t>,<c>,<s>` to
override RRF weights.

### 4.4 `GET /api/patients/{patient_id}/search` ***(planned S4)***

Patient-scoped variant: same logic as `/api/search/hybrid` but
pre-filtered on all Health Record resources (studies + reports +
consultations + documents + annotations) of the patient. Useful for
the Health Record UI "find within patient".

### 4.5 Tag API ***(planned S5)***

Endpoints:
- `GET /api/tags?target_kind&target_id`: list tags on the target.
- `POST /api/tags`: body `{target_kind, target_id, namespace, value}`.
- `DELETE /api/tags/{tag_id}`.
- `GET /api/tags/tree`: aggregation by `namespace` with counts,
  e.g.:
  ```json
  {
    "anatomy": {"lung": 123, "liver": 87, "brain": 45},
    "protocol": {"CT-chest": 210, "MR-brain": 58}
  }
  ```
- `GET /api/tags/autocomplete?q=lun&namespace=anatomy`: top-N
  `value` matching case-insensitively.

---

## 5. Reciprocal Rank Fusion (RRF)

Formula (Cormack et al. 2009):

```
RRF_score(d) = Σ_r  1 / (k + rank_r(d))
```

where:
- `d` is the document (study, series, report, ...),
- `r` iterates over the ranking lists (tsvector, BiomedCLIP, s-transformer),
- `rank_r(d)` is the 1-based position of document `d` in ranker `r`,
- `k` is a constant (default 60) that dampens the contribution of
  overly dominant top-1 entries.

**Weights.** In `/api/search/hybrid` ***(planned)*** the formula becomes:

```
RRF_weighted(d) = Σ_r  w_r / (k + rank_r(d))
```

Suggested defaults: `w_ts=1.0, w_clip=0.8, w_stext=1.2`. Tuning is
done per dataset. Empirical rule: raise `w_stext` if the corpus is in
Italian (the BiomedCLIP text encoder is weak on non-English).

**Tuning.** Start from uniform weights `(1,1,1)`, measure nDCG@10 on
a golden set of 50-100 annotated queries, and move one weight at a time
by ±0.2. The `k` parameter rarely needs tuning: 60 is robust.

---

## 6. Query embedding cache ***(planned)***

Each semantic query requires a text encoder forward-pass: ~50ms
CPU, dead time if the same query arrives 100 times. Strategy:

- Key: `qcache:sha256(model_id + ':' + normalized_query)`.
- Value: vector JSON (512 or 384 floats).
- TTL: 24h (repeated queries typically within the same session).
- Backend: Redis (already present for Arq).
- Invalidation: no aggressive time-based invalidation; the `model_id` in the
  key ensures that a model upgrade invalidates everything.

Query normalization: `strip → lower → collapse whitespace`. Do NOT
remove stopwords (BiomedCLIP uses them).

---

## 7. HNSW index tuning

Current index (migration 0002, line 272-274):

```sql
CREATE INDEX ix_embeddings_vector_cosine
ON embeddings USING hnsw (vector vector_cosine_ops)
```

Key pgvector HNSW parameters:

| Parameter         | Default | Effect                                        |
|-------------------|---------|-----------------------------------------------|
| `m`               | 16      | graph degrees. ↑ quality, ↑ memory, ↑ build|
| `ef_construction` | 64      | search width during build. ↑ recall, ↑ build time|
| `ef_search`       | 40      | search width at query time (runtime). ↑ recall, ↑ latency|

Per-session setting:

```sql
SET hnsw.ef_search = 100;   -- higher recall, ~2x latency
```

**Tradeoff.** On a dataset <100k vectors, `m=16 ef_construction=64
ef_search=40` (defaults) are enough for recall@10 > 0.98. Above 1M
vectors raise `m=32 ef_construction=200`, rebuilding the index
(O(N log N) operation, do it in a maintenance window).

**When to migrate to IVFFlat.** If the dataset exceeds 10M vectors and
RAM becomes the bottleneck: IVFFlat has a smaller footprint but
lower recall, acceptable when combined with RRF.

---

## 8. Troubleshooting

### "Similarity returns nothing"
Most common cause: the target series does not have an embedding yet. The
`GET /api/similar-to/{id}` returns 404 with detail
`"no embedding found for this target — run embedding generation first"`
(`backend/src/bvphoenix/api/search.py:170`).

Check:

```sql
SELECT model_id, COUNT(*) FROM embeddings
WHERE target_kind = 'series'
GROUP BY model_id;
```

If the series is missing, enqueue manually:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/series/$SERIES_ID/embed
```

Mass-embed endpoint ***(planned E6)***:
`POST /api/embeddings/embed-missing`, `POST /api/embeddings/retry-failed`.

### "Search is slow"
- Check statement timeout: `/api/search` sets
  `SET LOCAL statement_timeout = '3s'`
  (`search.py:56`). If the query times out the client sees 500.
- Verify the use of the tsvector index:
  ```sql
  EXPLAIN ANALYZE
  SELECT id FROM studies
  WHERE to_tsvector('simple', coalesce(study_description, ''))
        @@ plainto_tsquery('simple', 'ground-glass');
  ```
  It must show `Bitmap Index Scan on ix_studies_description_fts`.
- For similarity: check `ef_search`. If latency is fine but recall
  is not, raise `ef_search=100`.

### "Too many results / irrelevant"
- Adjust RRF weights (see §5).
- Add a tag-first pre-filter: the cascade is explicitly
  designed for this.
- Check `modality`/`body_part`: they are cheap filters that often
  remove 90% of the noise.

### "Coverage check" ***(planned E6)***
`GET /api/embeddings/coverage` will return:

```json
{
  "total_series": 1234,
  "embedded": 1190,
  "pending": 30,
  "failed": 14,
  "coverage_pct": 96.4
}
```

Use case: admin dashboard, alerting if `coverage_pct < 95%`.

---

## Code references

- `backend/src/bvphoenix/api/search.py`: search + similar-to endpoints.
- `backend/src/bvphoenix/db/models/embeddings.py`: embedding ORM.
- `backend/src/bvphoenix/db/models/annotations.py:99`: Tag model.
- `backend/alembic/versions/0002_auth_grants_annotations.py:272`:
  HNSW index + tsvector GIN.
- `workers/src/bvworkers/tasks/embed_series.py`: BiomedCLIP
  pipeline.
- `docs/architecture.md` §3: end-to-end similarity search flow.
- `docs/api-reference.md` §4: search API reference.
