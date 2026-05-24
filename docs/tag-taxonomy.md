# Tag taxonomy

The bit.vision tag system is a controlled vocabulary, organized
by **namespace**, that uniformly classifies imaging targets (studies,
series, instances) and curated datasets. Tags are the basis for: search
filters (`search_by_tags`), discovery within the Health Record, cohort
retrieval, and cross-references between modalities, anatomies, and
pathologies. Tags are the **cross-cutting** dimension of the three-axis
model (see [data-model.md §0](./data-model.md#0-conceptual-overview-the-three-axis-model)):
they label content for search without claiming a place on the timeline
(that is `ClinicalEvent`) or in a folder (that is `Folder`).

Each tag has the form `namespace:value` (e.g. `modality:CT`,
`anatomy:lung/upper-lobe`, `finding:nodule`). The value is always
**lowercase, singular, in English**: localization (Italian,
etc.) goes through the synonym/alias system described below.

---

## Standard namespaces

| Namespace | Purpose | Examples | Source |
|-----------|---------|----------|--------|
| `modality` | Imaging technique (DICOM Modality) | `CT`, `MR`, `XR`, `US`, `PT`, `NM`, `MG`, `OCT`, `DX`, `CR` | auto |
| `anatomy` | Region or organ. Supports sub-path `organ/sub-part` | `lung`, `heart`, `liver`, `brain`, `kidney`, `spine`, `lung/upper-lobe`, `heart/ventricle`, `spine/lumbar` | both |
| `body` | Macro anatomical zone (coarse-grained) | `thorax`, `abdomen`, `head`, `spine`, `pelvis`, `extremity`, `neck` | both |
| `finding` | Observed radiological finding | `nodule`, `mass`, `fracture`, `hemorrhage`, `edema`, `effusion`, `cyst`, `lesion`, `calcification`, `stenosis` | both |
| `pathology` | Diagnosis or clinical pathology | `pneumonia`, `cancer`, `stroke`, `embolism`, `ischemia`, `fibrosis`, `covid-19` | both |
| `technique` | Acquisition technique variant | `contrast`, `angio`, `diffusion`, `perfusion`, `native`, `t1`, `t2`, `flair` | auto |
| `laterality` | Side of the body | `left`, `right`, `bilateral` | both |

### Naming conventions
- Value always `lowercase`, singular, English
- Hierarchy separator: `/` (e.g. `anatomy:lung/upper-lobe`)
- No underscores: use `-` for compound words (`upper-lobe`)
- No spaces, no extended unicode

### Target kinds
A tag is bound to a `(target_kind, target_id)` pair. The
schema-enforced supported target kinds are exactly four: `study`,
`series`, `instance`, `dataset`. The CHECK constraint
`ck_tags_target_kind` (in
`backend/src/bvphoenix/db/models/annotations.py:91`) is the source of
truth.

The tag system is therefore **imaging-scoped today**: it labels DICOM
resources and curated dataset collections, not Documents,
ClinicalEvents, CarePhases, ReportContent rows, Markers, ClinicalNotes,
or Folders. The same `finding:nodule` can exist on multiple series
independently because the tuple `(target_kind, target_id, namespace,
value)` is unique per target.

Earlier versions of this file listed `report`, `annotation`,
`document`, and `consultation` as additional target kinds. Those
values are not in the schema. The `Report` entity was retired in v3 in
favour of `ReportContent`, and `Annotation` was split into `Marker`
and `ClinicalNote` in migration 0042 (see
`backend/src/bvphoenix/db/models/annotations.py:1-11`).

#### Before proposing to widen the constraint, model the need first

The imaging-only scope is a deliberate design choice, not a missing
feature. Tags fit imaging because the vocabulary is open
(`anatomy:*`, `finding:*`, `pathology:*`) and the source of truth is
the underlying artefact (referto, DICOM tag) from which the autotag
worker rederives them. On every other entity in the system, a tag
typically duplicates information that is already held more rigorously
elsewhere, creating a second source of truth that drifts.

Common temptations and the structured form they should take instead:

| Tempting tag | Real need | Correct form |
|---|---|---|
| `urgency:emergency`, `setting:inpatient`, `intent:diagnostic` on `ClinicalEvent` | Closed-vocabulary clinical attribute | Enum column on `ClinicalEvent` with `CHECK` constraint |
| `diagnosis:icc-perihilar` on a `Document` of kind `pathology_report` | Diagnosis carried by a structured report | `ReportContent` fields + cited `pathology:*` tags on the imaging targets |
| `outcome:NED-since-2024-09` on a `CarePhase` | Phase outcome at a point in time | `CarePhase.outcome` enum + dated narrative; do not duplicate as tag |
| `incident:phx-2024-08-29` across Document + Event + ReportContent | Causal sub-graph of the timeline | New `ClinicalEvent` of kind `incident_anchor` with `caused_by_event_id` FKs from related events |
| `legal-hold:claim-2024-XYZ` across arbitrary entities | GDPR / litigation hold | Dedicated `LegalHold(target_kind, target_id, claim_ref, started_at, released_at)` table |
| `study-cohort:ICC-trial-2026-arm-A` on Patient + Document + Event | Trial enrollment with temporal validity | `Trial` + `TrialEnrollment(trial_id, patient_id, enrolled_at, withdrawn_at, evidence_doc_id)` |
| `flag:audit-pending`, `flag:second-opinion-requested` | Workflow state with assignee and resolution | `ReviewRequest(target_kind, target_id, kind, requested_by, requested_at, resolved_at, resolved_by)` |

Widening `ck_tags_target_kind` is therefore a last resort. The
checklist before opening a migration:

1. Is the vocabulary open and high-cardinality (like `anatomy:*` is)?
   If no, prefer a column with `CHECK` constraint.
2. Is the information already carried, even partially, by a structured
   field, FK, or enum on the target table? If yes, a tag duplicates
   it; the structured form is the source of truth.
3. Does the use case need expiry, ownership, audit chain, or
   ritirabilità? If yes, prefer a dedicated table over a tag.
4. Is the value cross-entity (same identifier across multiple kinds)
   and does it need its own lifecycle? If yes, model it as a first-class
   entity (see `incident_anchor`, `LegalHold`, `Trial` examples above).
5. Only if none of the above apply, propose widening
   `ck_tags_target_kind` in a migration with the rationale, and update
   `docs/data-model.md §0` and this file in lockstep.

Documentation does not lead the schema; the schema does not lead
modelling either.

---

## Auto-tagging policy

When a target (study / series / instance / dataset) is created
or updated, the `autotag_target` worker is enqueued. It extracts tags via:

1. **Regex medical lexicon** (Italian + English): match of known
   radiology domain terms → tag with `source=auto`,
   `confidence=0.7-0.9`. Examples: `"polmone"` / `"lung"` →
   `anatomy:lung`; `"nodulo"` / `"nodule"` → `finding:nodule`.
2. **LLM fallback** for ambiguous cases when the variable
   `BVP_AUTOTAG_USE_LLM=true`. The model (provider configurable via
   `BVP_LLM_PROVIDER`) receives the target text and proposes
   additional tags with self-declared `confidence`.

### Manual tag precedence
Tags with `source=manual` **take precedence** and are never
overwritten or removed by the auto-tagging worker. The worker can
add orthogonal tags but cannot modify or delete manual ones.
This guarantees that a human intervention remains stable
across re-indexing and lexicon updates.

### Re-run
- `POST /api/tags/autotag?target_kind=X&target_id=Y` forces an ad-hoc
  re-run on a single target
- CLI `bvphoenix-autotag backfill --target-kind series --since 2026-01-01`
  for batch backfill

---

## Synonyms and aliasing

The primary value is always English lowercase; language
variants (Italian primarily) are managed as **aliases**.

- Preferred: `lung` (not `polmone` as primary value)
- Mapping via `POST /api/tags/alias` body
  `{namespace, primary, alias, locale?}`
- Examples:
  - `polmone` → alias of `lung` (locale=`it`)
  - `nodulo` → alias of `nodule` (locale=`it`)
  - `frattura` → alias of `fracture` (locale=`it`)
  - `ictus` → alias of `stroke` (locale=`it`)

Query endpoints (`search_by_tags`, `list_tags`, UI TagSelector)
automatically resolve the alias to the primary value before matching, so
that an Italian user can search for `"polmone"` and obtain
results tagged with `anatomy:lung`.

---

## How to add a tag manually

### UI
The **TagSelector** widget is available on the study page and in the
upload view. Autocomplete on existing values of the namespace, supports
creating new values with explicit confirmation (operator gating to
avoid proliferation).

### REST API
```http
POST /api/tags
{
  "target_kind": "study",
  "target_id": "3c7b2f41-1e28-4d04-9c63-0d7c2a5f8b90",
  "namespace": "finding",
  "value": "nodule"
}
```
The body can also be an array for bulk insert on the same target.
The created tag automatically has `source=manual`.

### MCP (Claude Desktop)
- Read: `search_by_tags` and `list_tags` are exposed as MCP tools
  (see [agent-protocols.md](./agent-protocols.md)).
- Write: **not exposed via MCP at the moment**. Tags are created only via
  UI or REST API. The motivation: avoid having an LLM generate tags
  automatically without operator validation. Auto-generated tags
  always go through the `autotag_target` worker which has a controlled
  lexicon.

---

## References

- Search / embeddings details: [search-and-embeddings.md](./search-and-embeddings.md)
- Agent protocols (MCP tool `search_by_tags`, `list_tags`):
  [agent-protocols.md](./agent-protocols.md)
- Data model (`tags` table): [data-model.md](./data-model.md)
- Conceptual placement (cross-cutting axis): [data-model.md §0](./data-model.md#0-conceptual-overview-the-three-axis-model)

*Audit 2026-05-03 corrected the supported `target_kind` list to reflect the actual `ck_tags_target_kind` constraint.*
