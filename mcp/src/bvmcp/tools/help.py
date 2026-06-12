"""Inline help reference for the BitVision MCP toolkit.

Single tool ``help`` returns a Markdown guide for the requested topic.
The intent is to give agents a self-serve way to discover platform
conventions (mention DSL, provenance fields, scope model) without
asking the human or guessing from tool schemas alone.

The guides are static strings checked into this module rather than
fetched from the backend: they document the *protocol* the MCP
toolkit expects, not runtime state, and shipping them inline lets the
LLM use them even when the network is jittery or the backend is in
maintenance.
"""

from __future__ import annotations

import json

from mcp.types import Tool

# ---------------------------------------------------------------------------
# Topic content
# ---------------------------------------------------------------------------

_INDEX = """\
# BitVision MCP — help index

Call ``help`` with one of the topics below to read the corresponding
guide. Topics are independent; pick the one closest to the next write
you are about to perform.

| Topic            | When to read                                      |
|------------------|---------------------------------------------------|
| ``markdown_links`` | Before writing any markdown body that should cite a study, document, folder, consultation, report, or tag (clinical notes, synthesis, ReportContent narrative, folder narrative, care-phase narrative). |
| ``agent_writes``   | Before invoking any write tool (``create_*``, ``update_*``, ``add_*``, ``replace_*``, ``ingest_*``, ``link_*``, ``cite_*``). Covers idempotency, ETag concurrency, dry-run, audit fields, and the ``author_kind='agent'`` provenance contract. |
| ``scopes_overview``| To understand which OAuth scopes gate which tools. Pair with ``get_my_scopes`` to know what your token can actually do. |
| ``annotation_kinds`` | Before calling ``write_annotation`` / ``update_annotation``. Lists every supported marker ``kind`` and the JSON ``geometry`` shape each one expects, with a copy-pasteable example per kind. |
| ``segmentations`` | Before calling ``auto_segment_series`` / ``predict_segmentation_interactive`` / ``upload_segmentation``. Covers the three voxel-level mask production paths (TotalSegmentator auto, MedSAM-2 click, raw NIfTI/NRRD upload), how to chain a ``bbox.lesion`` marker into a MedSAM-2 prompt, the persisted label conventions, and the open provenance gap that callers should mention in the audit body. |
"""

_MARKDOWN_LINKS = """\
# BitVision Markdown link DSL

Every markdown surface where the platform stores agent / human prose
(``patient.notes``, ``ClinicalNote.body``, ``Consultation.summary_md``,
``ReportContent.narrative_md``/``findings_md``/``recommendations_md``,
``Folder.description``/``narrative_md``, ``CarePhase.narrative_md``,
…) supports two cross-reference primitives layered on top of standard
Markdown:

  1. **Mentions** — clickable pills that link to a specific resource.
  2. **Tags** — clickable chips that link to the patient's tag view.

Both are validated server-side at write time. A body that names a
UUID belonging to **another** patient is rejected with
``HTTP 422 cross_patient_or_missing_link`` and the offending raw spans
are returned in the ``violations`` array. Always cite resources of
the **same** patient as the document you are writing into.

## Mention forms

Resource kinds: ``study``, ``series``, ``folder``, ``document``,
``consultation``, ``report``.

Each kind has two surface forms:

  * **Bare** — ``@kind:UUID``
    Example: ``@study:b8283209-56b4-4e47-b6a9-47328eb4772e``
    The renderer falls back to the kind label + short id when no
    title is provided.

  * **Link form** — ``[Title](@kind:UUID)``
    Example: ``[PET-TC total body 2026-04-03](@study:b8283209-56b4-4e47-b6a9-47328eb4772e)``
    Recommended whenever the agent has a meaningful human title; the
    rendered pill shows the title verbatim, which helps the human
    reviewer skim faster.

## Tag forms

Tags use the ``@tag:`` prefix (``#`` was retired because it collides
with markdown headings: a line starting with ``# `` is an H1, not a
tag).

  * **Bare** — ``@tag:value``
    Example: ``@tag:anatomy:lung``, ``@tag:finding:nodule``.
    Value charset: ``[A-Za-z0-9][A-Za-z0-9._/-]{0,63}``. Namespaced
    forms (``namespace:value``, e.g. ``anatomy:lung``) are encouraged.

  * **Link form** — ``[Title](@tag:value)``
    Example: ``[noduli polmonari](@tag:finding:nodule)``.

## End-to-end example

```markdown
La paziente è in follow-up per [linfoma B](@tag:diagnosis:lymphoma_b).

Lo studio chiave per il restaging è
[PET-TC total body 2026-04-03](@study:b8283209-56b4-4e47-b6a9-47328eb4772e),
referto integrale in
[Lettera di refertazione PET](@document:9f2e4b7a-1c3d-4e5f-a6b7-8c9d0e1f2a3b).

Vedi anche la [sintesi del consulto multidisciplinare](@consultation:c1d2e3f4-5a6b-7c8d-9e0f-1a2b3c4d5e6f).
```

## Server-side rules to remember

* The body MUST cite only resources of the patient that owns the
  containing artefact. Cross-patient citations are rejected and the
  whole save fails atomically.
* The UUID format is the standard ``8-4-4-4-12`` hex pattern (case
  insensitive).
* Mentions inside link text (``[Title](@kind:UUID)``) take precedence
  over bare scans on the same characters, so you cannot accidentally
  double-count a UUID.
* Markdown formatting (``**bold**``, ``# heading``, ``- list``,
  fenced code, blockquote, links) is rendered. ``EvidenceContent``
  walks every text node and splices the mention/tag pills back in,
  so DSL tokens inside ``**emphasis**`` keep working.
"""

_AGENT_WRITES = """\
# Agent-authored writes — provenance and concurrency

Every BitVision write tool (``create_*``, ``update_*``, ``add_*``,
``replace_*``, ``ingest_*``, ``link_*``, ``cite_*``, ``assign_*``)
records who initiated the change. When the caller is an MCP token,
the platform stamps the row as authored by an agent: the GUI surfaces
this as an "AI" badge and the audit trail keeps it forever. The
contract you must respect:

## Idempotency

Tools that *create* rows accept an ``Idempotency-Key`` header. Reuse
the same key for a logical retry: the platform returns the original
row instead of creating a duplicate. Never mint a fresh key when
retrying a 5xx — that races against the just-committed write.

## Optimistic concurrency (ETag / If-Match)

Tools that *update* rows require the ``etag`` returned by the most
recent read. The backend rejects stale writes with HTTP 412
``Precondition Failed``. The recovery path is:

  1. Read the row again to get a fresh ``etag``.
  2. Recompute the patch on top of the new state.
  3. Retry with the fresh ``etag``.

Never strip ETag enforcement (``If-Match: *``) to "make it work" —
that defeats the audit trail and can clobber a concurrent human edit.

## Dry-run

Most write tools accept ``dry_run=true``: the platform validates the
request (RBAC, schema, cross-patient invariants, vocabulary lookups)
and returns the *would-be* response without committing. Use this:

  * Before a multi-step batch to surface which step would fail.
  * When the user wants a preview before authorising the change.

## Provenance fields the platform fills automatically

You do **not** populate ``author_kind``, ``proposed_by_agent_id``,
``agent_token_id`` or ``agent_assistant_id``: the request middleware
sets them based on the bearer token. The fields surface in the audit
log and on the GUI badge so a clinician can always tell that a row
came from an agent. Trying to forge them in the request body is
ignored.

## Cross-patient writes are forbidden

Any write that references resources from more than one patient is
rejected at the service layer. This is enforced at multiple levels
(DB composite FK, API namespace 404, service kw-only ``patient_id``,
markdown link DSL validator). Always scope a write to a single
patient.

## Approval flow vs scope errors — disambiguate before retrying

Most MCP hosts (Claude.ai, Claude Desktop, agent CLIs) intercept
mutating tool calls and ask the human to approve each one. When the
user does not click *Approve* in the host's window the call is
short-circuited CLIENT-SIDE and the LLM receives the literal string
``No approval received``. The request never reaches this server, no
audit log entry is written, and the failure is unrelated to OAuth
scopes, RBAC, or patient-scope enforcement.

Genuine server-side denials look completely different: the response
is structured JSON with ``error: "backend_error"`` and ``http_status``
set to 401 (token problem) or 403 (scope/permission). Scope problems
additionally carry a ``required_scope`` field (see *Failure modes* in
``help(topic='scopes')``). Tell the cases apart by inspecting the
response payload:

  * Bare ``No approval received`` text → host approval flow. Ask the
    user to approve and retry the same call. Reuse the
    ``idempotency_key`` if you set one; the platform will return the
    original row on the eventual successful write.
  * JSON with ``error: "backend_error"`` and ``http_status: 401`` →
    token expired or revoked. Re-authenticate.
  * JSON with ``error: "backend_error"`` and ``http_status: 403`` →
    permission denied. The body's ``required_scope`` (or the message)
    tells you which scope is missing; remediation is operator-side
    (re-issue the token), not retry.

Do not surface "No approval received" to the user as a permission
error: it just means the approval prompt is still pending.

## Pair the writes with the markdown DSL

When the body field of the write you are performing is markdown
(``narrative_md``, ``description``, ``summary_md``, ``body``,
``findings_md``, ``recommendations_md``), populate it with the
mention / tag DSL described in ``help(topic="markdown_links")``. The
clinician will see the citations as clickable pills.
"""

_SCOPES_OVERVIEW = """\
# OAuth scopes — overview

The MCP toolkit is gated by per-capability scopes. The token your
client received from ``/oauth/token`` carries a subset of them; tools
the token cannot serve are filtered out at ``list_tools()`` time so
you only see what you can call.

Call ``get_my_scopes`` (no arguments) to see the exact list your
token holds.

Common scope buckets:

  * ``read:metadata`` — patient demographics, study/document/folder
    metadata, search, summaries, bundle.
  * ``read:pixels`` — DICOM slices, thumbnails, image-level access.
  * ``write:annotations`` / ``write:notes`` — agent reading notes
    and clinical-note rows.
  * ``write:reports`` — ReportContent (extracted, derived, synthesis
    drafts). The signature transition (``sign_report_content``) is
    HUMAN-ONLY: agent tokens never carry it.
  * ``write:documents`` — document ingest / merge / supersede.
  * ``write:tags`` / ``write:metadata`` — study/document/folder
    metadata edits.
  * ``phases:propose`` / ``phases:write`` — care-phase classifier
    output and direct phase edits respectively. ``phases:propose``
    is read-mostly: the agent drafts, the human applies.
  * ``finalize:consultations`` — consultation signature. Agents
    never carry this scope; consultations always sign through a
    human gate.
  * ``admin:embeddings`` — platform-wide embedding maintenance
    (coverage reads + enqueueing missing/failed re-embeds, including
    per-model text-chunk re-embeds). Sensitive, and only effective
    when the assistant's OWNER is a platform admin: the backend
    re-checks ownership on every call.

## Failure modes

  * Tool present, scope missing → HTTP 403 with a structured
    ``required_scope`` field. The remediation is operator-side
    (re-issue the token with the missing scope), not a retry.
  * Tool absent from ``list_tools()`` → either the scope is missing
    on the token, or the backend has the feature flag off. Check
    ``get_my_scopes`` first; if the scope is there but the tool is
    not, it is a feature-flag gate.
"""

_ANNOTATION_KINDS = """\
# Marker / annotation kinds — vocabulary and geometry shapes

Every annotation written through ``write_annotation`` lands in the
``markers`` table. The ``kind`` field is a closed vocabulary backed
by a CHECK constraint: unknown values are rejected with HTTP 422 and
the response ``ctx.allowed_kinds`` lists every accepted value. The
``geometry`` payload is a JSON object whose shape depends on
``kind``; the server stores it as JSONB and does not deep-validate
inner fields, so it is the caller's job to use the right shape per
kind.

Coordinates are voxel indices ``(i, j, k)`` into the series volume
the viewer streams; ``axis`` (when present) names the slicing plane
the geometry was authored on. ``computed`` is a free-form object for
derived values (length / area / SUV / volume_ml + their units).

## Measurement kinds

All ``measurement.*`` geometries share the convention
``{"axis": "axial"|"coronal"|"sagittal", "points": [[i,j,k], ...]}``.
The number of points is kind-specific.

### ``measurement.distance`` (2 points)

```json
{
  "axis": "axial",
  "points": [[120, 80, 42], [134, 80, 42]]
}
```

### ``measurement.angle`` (3 points, vertex = middle)

```json
{
  "axis": "axial",
  "points": [[100, 60, 42], [120, 70, 42], [140, 60, 42]]
}
```

### ``measurement.area`` (closed polygon, ≥3 points)

```json
{
  "axis": "axial",
  "points": [[110, 50, 42], [140, 50, 42], [140, 80, 42], [110, 80, 42]]
}
```

### ``measurement.ellipse`` (2 points = opposite corners of the AABB)

```json
{
  "axis": "axial",
  "points": [[110, 50, 42], [140, 80, 42]]
}
```

### ``measurement.freehand`` (open or closed polyline, ≥2 points)

```json
{
  "axis": "axial",
  "points": [[110, 50, 42], [115, 55, 42], [122, 58, 42], "..."]
}
```

### ``measurement.arrow`` (2 points: tail, head)

```json
{
  "axis": "axial",
  "points": [[110, 50, 42], [140, 70, 42]]
}
```

### ``measurement.text`` (1 anchor point + ``body`` field)

```json
{
  "axis": "axial",
  "points": [[120, 80, 42]]
}
```

### ``measurement.probe`` (1 point — single-voxel value probe)

```json
{
  "axis": "axial",
  "points": [[120, 80, 42]]
}
```

### ``measurement.bbox`` (2 points = opposite corners on a single slice)

2D box drawn on one slice. For a 3D bounding box that wraps a lesion
across many slices, use ``bbox.lesion`` instead.

```json
{
  "axis": "axial",
  "points": [[110, 50, 42], [140, 80, 42]]
}
```

## ``bbox.lesion`` — 3D axis-aligned lesion bounding box

The natural sink for the output of ``find_hot_spots``: each hot spot
returns ``bbox_min_ijk`` / ``bbox_max_ijk`` which can be copied
verbatim into ``geometry``. ``computed`` SHOULD carry the source
metrics so the viewer can label the box without re-running the
discovery tool.

```json
{
  "min_ijk": [90, 151, 130],
  "max_ijk": [104, 166, 147]
}
```

Recommended ``computed`` shape when the source is ``find_hot_spots``:

```json
{
  "suv_max": 9.4,
  "suv_mean": 5.1,
  "volume_ml": 3.2,
  "voxel_count": 256,
  "source": "find_hot_spots"
}
```

The viewer's marker panel jumps to the box centroid on click. A full
3D wireframe overlay is on the follow-up list; until then the box is
visible as a list entry with click-to-locate (no in-canvas outline).

## ``bbox.exclusion`` — 3D region excluded from ROI / hot-spot search

Same geometry shape as ``bbox.lesion``. Pass the marker id to
``compute_roi_stats`` / ``find_hot_spots`` via ``exclude_marker_ids``
to subtract this region from the analysis. Day-1 fallback when no
automatic segmentation mask is available (e.g. PET without a CT
companion, or production ARM64 cluster before the TotalSegmentator
wheel is unblocked).

```json
{
  "min_ijk": [90, 151, 130],
  "max_ijk": [104, 166, 147]
}
```

The preferred mechanism on series with a TotalSegmentator output is
``exclude_segmentation_labels=["kidney_left","kidney_right",
"urinary_bladder"]`` — anatomic, deterministic, reusable.

## Viewer "Lens probe" → ``compute_roi_stats`` (no new tool)

The viewer's Lens probe (live circular ROI cursor with mean/std/
min/max and SUV-mean readout) is a UI affordance over
``compute_roi_stats(kind='sphere', center_ijk=..., radius_mm=...)``.
The 2D disc on the active slice is pinned as a 3D sphere of the
same radius — clinically more useful than a single-slice disc
because it catches partial-volume into adjacent slices, and avoids
a separate ``kind=disk`` schema. When the operator clicks to
persist, the viewer writes a ``measurement.ellipse`` marker with
``computed.source = "lens-probe"`` and the radius / stats inline:

```json
{
  "radius_mm": 5.0,
  "voxel_count": 37,
  "mean": 42.1,
  "std": 18.2,
  "suv_mean": 4.2,
  "suv_max": 9.1,
  "suv_variant": "bw",
  "source": "lens-probe"
}
```

## ``fiducial`` — single 3D landmark point

```json
{
  "point": [120, 80, 42]
}
```

## ``reading-note`` — viewport-anchored free-text bookmark

Optional anchor + free-text body (set via the top-level ``body``
field, not inside ``geometry``).

```json
{
  "anchor": [120, 80, 42]
}
```

## ``text-overlay`` — text drawn on the image

```json
{
  "axis": "axial",
  "anchor": [120, 80, 42]
}
```

## End-to-end example — round-trip ``find_hot_spots`` into a marker

```text
1. spots = find_hot_spots(series_id=…, top_n=5)
2. for spot in spots:
       write_annotation(
           patient_id=…,
           target_kind="series",
           target_id=…,
           kind="bbox.lesion",
           geometry={"min_ijk": spot.bbox_min_ijk,
                     "max_ijk": spot.bbox_max_ijk},
           computed={"suv_max": spot.suv_max,
                     "volume_ml": spot.volume_ml,
                     "voxel_count": spot.voxel_count,
                     "source": "find_hot_spots"},
           body=f"Hot spot #{spot.rank} (SUVmax={spot.suv_max:.1f})",
       )
```

## Failure mode reference

* Unknown ``kind`` → HTTP 422 with body
  ``{"detail": [{"loc": ["body", "kind"], "ctx":
  {"allowed_kinds": [...]}, ...}]}``.
* Missing scope → HTTP 403 with ``required_scope``.
* Cross-patient ``target_id`` → HTTP 404 (the patient namespace
  hides cross-patient targets by construction).
* Set ``dry_run=true`` to validate the call (RBAC + kind + patient)
  without writing the row; the response is the would-be ``MarkerOut``
  with ``id="dry-run"``.
"""


_SEGMENTATIONS = """\
# Segmentations — voxel-level mask production for training data

A ``Marker`` (kind ``measurement.area`` or ``measurement.freehand``)
is a 2D polyline on a single slice: it is a *contour*, not a *mask*.
Useful as a viewer landmark, marginal as training input for a
segmentation network. To produce true 3D voxel-level masks the
platform exposes three write surfaces, gated under
``imaging:compute`` (auto + interactive — they trigger expensive
worker passes) or ``annotations:write`` (external mask upload —
data-only).

## When to use which

| Tool | Use when | Latency | Output |
|------|----------|---------|--------|
| ``auto_segment_series`` | You want anatomical priors over the whole volume (organs, vessels) before zooming into lesions. CT-only today. | async (poll ``get_segmentations``) | one mask per ROI, semantic labels |
| ``predict_segmentation_interactive`` | You have a candidate lesion (e.g. a ``bbox.lesion`` marker or a click) and want a precise contour. | sync, ~3-10 s CPU, 60 s timeout | 2D mask on the chosen slice; persists as full-volume binary when ``label`` is set |
| ``upload_segmentation`` | The mask was produced outside the platform (Slicer, ITK-SNAP, custom worker) or the agent computed it deterministically (e.g. SUV threshold). NIfTI / NRRD / DICOM SEG. | sync, ≤ 200 MiB | one mask under the chosen ``label`` |

## Recommended agent loop (discovery → mask → training-ready label)

```text
1. spots = find_hot_spots(series_id=…, top_n=5, min_volume_ml=1.0)
2. for spot in spots:
     a. write_annotation(
            patient_id=…,
            target_kind="series",
            target_id=series_id,
            kind="bbox.lesion",
            geometry={"min_ijk": spot.bbox_min_ijk,
                      "max_ijk": spot.bbox_max_ijk},
            computed={"suv_max": spot.suv_max,
                      "volume_ml": spot.volume_ml,
                      "source": "find_hot_spots"},
            body=f"Hot spot #{spot.rank}",
        )
     b. cz = (spot.bbox_min_ijk[2] + spot.bbox_max_ijk[2]) // 2  # axial centroid
        cx = (spot.bbox_min_ijk[0] + spot.bbox_max_ijk[0]) // 2
        cy = (spot.bbox_min_ijk[1] + spot.bbox_max_ijk[1]) // 2
        predict_segmentation_interactive(
            series_id=series_id,
            axis=2, slice_idx=cz,
            # one centroid click + 4 corners as background hints
            points=[[cx, cy],
                    [spot.bbox_min_ijk[0], spot.bbox_min_ijk[1]],
                    [spot.bbox_max_ijk[0], spot.bbox_min_ijk[1]],
                    [spot.bbox_min_ijk[0], spot.bbox_max_ijk[1]],
                    [spot.bbox_max_ijk[0], spot.bbox_max_ijk[1]]],
            labels=[1, 0, 0, 0, 0],
            label=f"lesion_{spot.rank:02d}",
        )
3. (training export — server-side)
   GET /series/{id}/segmentations  →  list of persisted labels
   GET /series/{id}/segmentations/{label}  →  raw uint8 mask buffer
```

The persisted ``label`` should be descriptive but URL-safe:
``[a-zA-Z0-9._-]{1,64}``. Suggested convention:
``{anatomical_site}_{index:02d}`` (e.g. ``liver_lesion_03``,
``iliac_node_01``) so the dataset export can group by site without
parsing prose.

## Coordinate frames at a glance

* ``find_hot_spots`` returns ``bbox_min_ijk`` / ``bbox_max_ijk`` and
  ``centroid_ijk`` in voxel indices ``[i, j, k]`` into the packed
  Float32 volume the viewer streams. Same convention as
  ``compute_roi_stats`` / ``write_annotation`` geometry.
* ``predict_segmentation_interactive`` ``points`` are in IN-SLICE
  pixel coordinates ``[x, y]`` of the slice picked by
  ``axis`` + ``slice_idx``. For ``axis=2`` (axial),
  ``x ↔ i``, ``y ↔ j``; for ``axis=1`` (coronal),
  ``x ↔ i``, ``y ↔ k``; for ``axis=0`` (sagittal),
  ``x ↔ j``, ``y ↔ k``. Pass the matching pair, not the full triple.

## Provenance — what is recorded today vs. the open gap

Recorded automatically:

* Audit log entry per call (``actor_subject_id``,
  ``agent_token_id`` when the caller is an MCP token).
* For ``predict_segmentation_interactive`` with a ``label``: the
  persisted volume's storage key under
  ``segmentations/{series_id}/{label}.bin``.

NOT recorded today (open follow-up — flag in any training export):

* ``producer`` / ``producer_version`` / ``model_id`` / ``provider``
  per persisted mask. The legacy ``.bin`` registry is keyed by label
  only; promoting these fields onto the ``Segmentation`` ORM row is
  a separate backend change.
* Cross-link between a ``bbox.lesion`` marker and the mask that was
  prompted from it. The link is reconstructible from the audit
  trail + label naming convention, but it is not enforced in the
  schema.

While the provenance gap is open, mirror the relevant metadata in
the marker's ``computed`` and ``body`` fields (model name, prompt
points used, segmentation label) so a downstream training pipeline
can join on (series_id, label) without scraping the audit log.

## Failure modes (caller-side)

* CT-only TotalSegmentator: the worker rejects non-CT modalities
  with a structured error in the job result. Check ``modality`` on
  the series before enqueuing.
* MedSAM-2 unavailable: the predict endpoint returns HTTP 502 with
  the worker's error verbatim (typically a missing ``seg`` extra).
  Surface it to the user instead of retrying.
* Upload >200 MiB: the MCP tool rejects client-side before the
  network round-trip. Compress the NIfTI (``.nii.gz``) or split into
  per-label files.
* Label collision: existing label is silently overwritten unless
  the calling endpoint exposes an explicit overwrite flag
  (``auto_segment_series.overwrite`` does; the legacy upload path
  does not). Choose label names that include a discriminator (rank,
  date) when running iteratively.
"""


_TOPICS: dict[str, str] = {
    "index": _INDEX,
    "markdown_links": _MARKDOWN_LINKS,
    "agent_writes": _AGENT_WRITES,
    "scopes_overview": _SCOPES_OVERVIEW,
    "annotation_kinds": _ANNOTATION_KINDS,
    "segmentations": _SEGMENTATIONS,
}

# ---------------------------------------------------------------------------
# Tool definition + dispatcher
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="help",
        description=(
            "Inline reference for the BitVision MCP toolkit. Returns a "
            "Markdown guide on the requested topic. Call with no args "
            "(or topic='index') to list available topics. Read this "
            "before drafting markdown bodies (see topic='markdown_links') "
            "or invoking write tools (topic='agent_writes')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": sorted(_TOPICS.keys()),
                    "description": (
                        "Help topic to render. Defaults to 'index' which "
                        "lists every available topic with a short hint."
                    ),
                    "default": "index",
                },
            },
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name != "help":
        return json.dumps({"error": f"unknown help tool: {name}"})
    topic = (arguments or {}).get("topic") or "index"
    body = _TOPICS.get(topic)
    if body is None:
        return json.dumps(
            {
                "error": f"unknown topic: {topic!r}",
                "available_topics": sorted(_TOPICS.keys()),
            }
        )
    return body
