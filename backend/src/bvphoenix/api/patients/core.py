# ruff: noqa: F405, B008
# Auto-split from api/patients.py on 2026-05-21.
# Section: ``core``. Decorators register against the
# local ``router`` below; the package __init__.py
# aggregates every child via include_router so main.py's
# wiring stays a single line.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import _shared  # for runtime access
from bvphoenix.api.patients._shared import *  # noqa: F403
from bvphoenix.services.text_embedding import enqueue_text_embed, patient_embed_text

router = APIRouter()


@router.get("/patients", response_model=PaginatedPatients)
async def list_patients(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=255),
    scope: str = Query(
        "personal",
        description=(
            "Subset of the visible set: personal=mine+shared (default), "
            "mine=managed/self, shared=via Grant, public=open-data datasets, "
            "all=no extra filter on top of visibility."
        ),
    ),
    tag: str | None = Query(
        None,
        max_length=320,
        description=(
            "Filter to patients owning at least one study (or series/instance "
            "under that study) carrying the given tag. Format: ``namespace:value``."
        ),
    ),
) -> PaginatedPatients:
    if scope not in _PATIENT_SCOPES:
        raise HTTPException(status_code=422, detail=f"invalid scope {scope!r}")
    base = await visible_patients_filter(db, user)
    if tag:
        ns, _, val = tag.partition(":")
        if not ns or not val:
            raise HTTPException(
                status_code=422,
                detail="tag must be 'namespace:value'",
            )
        # Walk Tag → owning ImagingStudy via the three target kinds. We match on
        # study/series/instance separately and union the resulting study
        # ids; then keep patients owning any such study. ``dataset`` tags
        # are ignored — they don't bind to a single patient.
        study_ids_subq = (
            select(ImagingStudy.id)
            .where(
                ImagingStudy.id.in_(
                    select(Tag.target_id).where(
                        Tag.namespace == ns,
                        Tag.value == val,
                        Tag.target_kind == "study",
                    )
                )
                | ImagingStudy.id.in_(
                    select(Series.study_id)
                    .join(Tag, Tag.target_id == Series.id)
                    .where(
                        Tag.namespace == ns,
                        Tag.value == val,
                        Tag.target_kind == "series",
                    )
                )
                | ImagingStudy.id.in_(
                    select(Series.study_id)
                    .join(Instance, Instance.series_id == Series.id)
                    .join(Tag, Tag.target_id == Instance.id)
                    .where(
                        Tag.namespace == ns,
                        Tag.value == val,
                        Tag.target_kind == "instance",
                    )
                )
            )
            .scalar_subquery()
        )
        base = base.where(
            Patient.id.in_(
                select(ImagingStudy.patient_id).where(ImagingStudy.id.in_(study_ids_subq))
            )
        )
    if user is not None and scope != "all":
        owner_clause = or_(
            Patient.managed_by_subject_id == user.subject_id,
            Patient.self_user_subject_id == user.subject_id,
        )
        public_clause = Patient.managed_by_subject_id == platform_owner_subject_id()
        if scope == "mine":
            base = base.where(owner_clause)
        elif scope == "public":
            base = base.where(public_clause)
        elif scope == "shared":
            # "Visible but not mine and not the open-data dataset" — by
            # construction the remainder of ``visible_patients_filter``
            # is anything reaching the user via a Grant.
            base = base.where(~owner_clause).where(~public_clause)
        elif scope == "personal":
            # Default. Hide open-data datasets from the everyday list;
            # they remain reachable via the explicit "public" scope.
            base = base.where(~public_clause)
    if q:
        # v3: tax_id + external_id columns dropped (lives in
        # external_identifiers JSONB). FTS over the JSONB text plus
        # the materialised ``cf_normalized`` column keeps the search
        # working without re-introducing the legacy columns.
        ext_text = func.cast(Patient.external_identifiers, Text)
        base = base.where(
            or_(
                Patient.display_name.ilike(f"%{q}%"),
                Patient.cf_normalized.ilike(f"%{q.upper()}%"),
                ext_text.ilike(f"%{q}%"),
            )
        )
    count_query = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_query)).scalar_one()
    rows = (
        (await db.execute(base.order_by(Patient.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return PaginatedPatients(
        items=[_patient_out(p, user=user) for p in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post("/patients", response_model=PatientOut, status_code=status.HTTP_201_CREATED)
async def create_patient(
    request: Request,
    body: PatientCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> PatientOut:
    # Registering a brand-new fascicolo is a clinical onboarding step
    # that must stay human-driven: an agent token is bound to a fixed
    # ``patient_ids`` allow-list, and a freshly created patient would
    # not even be readable by the agent that just minted it. Refuse
    # explicitly so a leaked token cannot mass-create demographic rows.
    if getattr(request.state, "is_agent", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agents cannot register new patients (human-only)",
        )
    # v3: tax_id + external_id columns dropped; build the
    # external_identifiers JSONB array from the legacy body fields so
    # the existing client surface keeps creating patients with the
    # CF / DICOM PatientID it carries today. The contacts JSONB
    # column was also dropped (the relational patient_contacts table
    # is the source of truth); we persist contacts separately below.
    external_identifiers: list[dict] = []
    if body.tax_id:
        external_identifiers.append(
            {
                "system": "urn:oid:2.16.840.1.113883.2.9.4.3.2",
                "value": body.tax_id,
                "type": "fiscal-code",
                "assigner": "Agenzia delle Entrate",
            }
        )
    if body.external_id:
        external_identifiers.append(
            {
                "system": "DICOM:Issuer:UNKNOWN",
                "value": body.external_id,
                "type": "MR",
            }
        )
    patient = Patient(
        managed_by_subject_id=user.subject_id,
        display_name=body.display_name,
        birth_date=body.birth_date,
        sex=body.sex,
        phone=body.phone,
        email=body.email,
        address=body.address,
        blood_type=body.blood_type,
        birth_place_city=body.birth_place_city,
        birth_place_province=body.birth_place_province,
        asl_code=body.asl_code,
        asl_name=body.asl_name,
        allergies=body.allergies,
        notes=body.notes,
        external_identifiers=external_identifiers,
    )
    db.add(patient)
    await db.flush()
    await db.refresh(patient)
    # Materialise the patient root folder in the same transaction.
    # Every subsequent document ingestion that omits ``folder_id``
    # falls back to this root, preserving the no-orphan invariant
    # enforced by ``trg_folder_items_no_orphan_doc``.
    from bvphoenix.services.folders import get_or_create_root_folder

    await get_or_create_root_folder(db, patient)
    # Seed main + record the initial demographic snapshot so the
    # Versions tab is populated from creation. Same transaction as the
    # row insert: a versioning failure here aborts the whole create.
    await seed_patient_main(db, patient=patient, user=user, request=request)
    await record_versioned_change(
        db,
        patient=patient,
        user=user,
        request=request,
        entity_kind="patient",
        entity_id=patient.id,
        payload=_patient_versioning_payload(patient),
        message="[patient] create",
    )
    # Persist the initial contact list into the dedicated 1:N table so
    # the new schema is the source of truth from the very first save.
    if body.contacts:
        from bvphoenix.services.patient_contacts import replace_all_contacts

        await replace_all_contacts(
            db,
            patient_id=patient.id,
            incoming=[c.model_dump() for c in body.contacts],
        )
    await db.commit()
    await db.refresh(patient)
    await enqueue_text_embed(
        db, target_kind="patient", target_id=patient.id, text=patient_embed_text(patient)
    )
    contacts = await _load_patient_contacts(db, patient.id)
    return _patient_out(patient, user=user, contacts=contacts)


@router.get("/patients/{patient_id}", response_model=PatientOut)
async def get_patient(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> PatientOut:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    await audit.log(
        action="patient_view",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="patient",
        resource_id=patient.id,
    )
    contacts = await _load_patient_contacts(db, patient.id)
    editor_name = await _resolve_notes_editor_name(db, patient.notes_updated_by_subject_id)
    out = _patient_out(
        patient,
        user=user,
        contacts=contacts,
        notes_editor_display_name=editor_name,
    )
    # Surface the current main-branch commit hash so callers can chain
    # a read into a PATCH with ``If-Match`` without a separate ETag
    # round-trip.
    out.etag = await etag_for_branch(db, patient_id=patient.id, ref_name="main")
    return out


@router.patch("/patients/{patient_id}", response_model=PatientOut)
async def update_patient(
    request: Request,
    patient_id: uuid.UUID,
    body: PatientUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: Annotated[bool, Depends(dry_run_flag)] = False,
) -> PatientOut:
    """Edit a patient's anagrafica (demographics).

    Mutating contract (mirrors document update):

    * Agent tokens require the ``patient:write`` scope. Human users go
      through the standard ownership / grant check.
    * ``If-Match`` (optional): when present, must equal the current
      ``etag`` of the patient's main branch; otherwise 412.
    * ``Idempotency-Key`` (optional): replays the captured response for
      24h. Same key + different body returns 422.
    * ``?dry_run=true``: returns the diff without committing or
      auditing.
    """
    enforce_agent_scope(request, "patient:write")
    if idem.replay is not None:
        return idem.replay  # type: ignore[return-value]

    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if DELETE not in perms and READ_METADATA not in perms:
        raise HTTPException(status_code=403, detail="cannot update this patient")

    updates = body.model_dump(exclude_unset=True)
    # ``contacts`` is no longer a column on ``patients``; it lives in
    # the dedicated 1:N table. Pop it out of the column-update map so
    # ``setattr`` doesn't try to assign it onto the ORM row, and
    # remember the array so we can apply replace-all semantics on the
    # table after the column update commits.
    contacts_payload = updates.pop("contacts", None)

    # ``tax_id`` and ``external_id`` are NOT columns on ``patients`` in
    # v3 — they live as entries in ``external_identifiers`` (JSONB
    # array). The naive ``setattr`` loop below would silently drop them
    # (no error, no persistence, response shows ``null``), which the
    # MCP agent surfaced as a confusing partial-write. Pull them out
    # explicitly so we can either persist them through the JSONB upsert
    # or refuse with 403 when the agent is missing the dedicated
    # ``patients:identify`` scope.
    #
    # Legacy PATCH semantics here are deliberately upsert-only: the
    # frontend edit form sends every field on save (including ``tax_id``
    # = ``null`` when the user did not touch it but the state slice
    # carried no value), so treating ``null`` / ``""`` as "remove the
    # entry" used to silently delete identifiers added via the dedicated
    # ``POST /external-identifiers`` endpoint after the user happened to
    # submit the patient form. To remove an identifier the caller MUST
    # use ``DELETE /api/patients/{id}/external-identifiers`` — the legacy
    # body fields cannot delete.
    legacy_identifier_updates: dict[str, str] = {}
    if "tax_id" in updates:
        raw = updates.pop("tax_id")
        if isinstance(raw, str) and raw.strip():
            legacy_identifier_updates["fiscal-code"] = raw.strip()
    if "external_id" in updates:
        raw = updates.pop("external_id")
        if isinstance(raw, str) and raw.strip():
            legacy_identifier_updates["MR"] = raw.strip()
    if legacy_identifier_updates:
        # Identifier writes cross-cut patient identity (a wrong CF
        # links the wrong fascicolo, a wrong MR mis-routes DICOM
        # imports). Require the granular ``patients:identify`` scope
        # rather than the broader ``patient:write`` so the operator
        # opts in deliberately when granting this capability to an
        # AI assistant. The legacy ``patient:identify`` singular form
        # is honoured for backward compat.
        enforce_agent_scope(request, "patients:identify", "patient:identify")

    current_etag = await etag_for_branch(db, patient_id=patient.id, ref_name="main")
    require_if_match(request, current_etag)

    diff = _patient_diff(patient, updates)

    # Project the would-be state to run the codice-fiscale consistency
    # check against, so the warning reflects the *post-edit* fields not
    # what the row carries today. The validator is non-blocking by
    # design: it surfaces mismatches as a warning array on the
    # response and the human (or agent) decides whether to honour
    # them. CFs and demographic forms can legitimately disagree
    # (changed surname, foreign-born adopted, …).
    from bvphoenix.services.codice_fiscale import check_consistency as _cf_check

    # v3: tax_id is no longer a column; derive it from the
    # external_identifiers JSONB so the CF consistency check still
    # has something to match against. ``updates`` may carry a tax_id
    # field from the legacy frontend slice — if so, prefer it.
    legacy_cf: str | None = None
    for entry in patient.external_identifiers or []:
        if isinstance(entry, dict) and entry.get("type") == "fiscal-code":
            v = entry.get("value")
            if isinstance(v, str):
                legacy_cf = v
                break
    projected = {
        "tax_id": updates.get("tax_id", legacy_cf),
        "birth_date": updates.get("birth_date", patient.birth_date),
        "sex": updates.get("sex", patient.sex),
    }
    cf_warnings = [
        {
            "field": w.field,
            "stored": w.stored,
            "decoded_from_cf": w.decoded_from_cf,
            "detail": w.detail,
        }
        for w in _cf_check(
            projected["tax_id"],
            birth_date=projected["birth_date"],
            sex=projected["sex"],
        )
    ]

    if dry_run:
        # Don't reconcile the contacts table on dry-run; the response
        # echoes the would-be contacts list straight from the payload
        # so the caller can preview without touching the DB.
        current_contacts = await _load_patient_contacts(db, patient.id)
        out_payload = _patient_out(patient, user=user, contacts=current_contacts).model_dump()
        out_payload["etag"] = current_etag
        out_payload["diff"] = diff
        out_payload["dry_run"] = True
        if cf_warnings:
            out_payload["cf_warnings"] = cf_warnings
        if contacts_payload is not None:
            out_payload["contacts_preview"] = contacts_payload
        return idem.capture(out_payload, status_code=200)  # type: ignore[return-value]

    for field, value in updates.items():
        setattr(patient, field, value)
    # Stamp notes-specific provenance only when the notes field
    # actually changed. Comparing against the pre-PATCH snapshot
    # would be more precise, but ``updates`` only contains fields
    # the body explicitly carried, so its presence here is a
    # sufficient proxy: a no-op PATCH of the same value still
    # bumps the timestamp, which matches the user expectation
    # ("ho appena premuto Salva, l'orario deve aggiornarsi").
    if "notes" in updates:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        patient.notes_updated_at = _datetime.now(_UTC)
        patient.notes_updated_by_subject_id = user.subject_id
    if legacy_identifier_updates:
        # Upsert ``fiscal-code`` / ``MR`` entries on the JSONB array.
        # ``legacy_identifier_updates`` is populated above only when the
        # user supplied a non-empty value, so a missing / empty
        # ``tax_id`` body field is a no-op here. Other identifier types
        # (``passport``, ``Issuer:*``, …) on the same row are left
        # untouched so the column-level PATCH never blows away
        # identifiers that came in via ``link_external_identifier`` or
        # the dedicated ``/external-identifiers`` endpoint.
        existing = list(patient.external_identifiers or [])
        new_array: list[dict] = [
            entry
            for entry in existing
            if not (isinstance(entry, dict) and entry.get("type") in legacy_identifier_updates)
        ]
        for type_key, value in legacy_identifier_updates.items():
            if type_key == "fiscal-code":
                new_array.append(
                    {
                        "system": "urn:oid:2.16.840.1.113883.2.9.4.3.2",
                        "value": value,
                        "type": "fiscal-code",
                        "assigner": "Agenzia delle Entrate",
                    }
                )
            elif type_key == "MR":
                new_array.append(
                    {
                        "system": "DICOM:Issuer:UNKNOWN",
                        "value": value,
                        "type": "MR",
                    }
                )
        patient.external_identifiers = new_array
    await db.flush()
    if contacts_payload is not None:
        from bvphoenix.services.patient_contacts import replace_all_contacts

        await replace_all_contacts(
            db,
            patient_id=patient.id,
            incoming=contacts_payload,
        )
    fields_in_message = sorted(list(updates.keys()) + list(legacy_identifier_updates.keys()))
    commit = await record_versioned_change(
        db,
        patient=patient,
        user=user,
        request=request,
        entity_kind="patient",
        entity_id=patient.id,
        payload=_patient_versioning_payload(patient),
        message=f"[patient] edit ({', '.join(fields_in_message) or 'no-op'})",
    )
    await db.commit()
    await db.refresh(patient)
    await enqueue_text_embed(
        db, target_kind="patient", target_id=patient.id, text=patient_embed_text(patient)
    )

    new_etag = commit.commit_hash.hex()

    # Only record which fields changed — PHI scrubbing in the service
    # layer handles the values if they ever leak in.
    await audit.log(
        action="patient_update",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient.id,
        metadata={
            "fields_updated": sorted(list(updates.keys()) + list(legacy_identifier_updates.keys())),
            "contacts_replaced": contacts_payload is not None,
            "identifiers_updated": sorted(legacy_identifier_updates.keys()),
            "etag": new_etag,
        },
    )
    fresh_contacts = await _load_patient_contacts(db, patient.id)
    editor_name = await _resolve_notes_editor_name(db, patient.notes_updated_by_subject_id)
    out = _patient_out(
        patient,
        user=user,
        contacts=fresh_contacts,
        notes_editor_display_name=editor_name,
    )
    out.etag = new_etag
    payload = out.model_dump()
    if cf_warnings:
        payload["cf_warnings"] = cf_warnings
    return idem.capture(  # type: ignore[return-value]
        payload,
        status_code=200,
        extra_headers={"ETag": format_etag(new_etag)},
    )


@router.get(
    "/patients/_decode_cf",
    response_model=CFDecodeOut,
)
async def decode_patient_cf(
    cf: str = Query(min_length=1, max_length=32),
    birth_date: date | None = Query(default=None),
    sex: str | None = Query(default=None, max_length=1),
    birth_place_belfiore: str | None = Query(default=None, max_length=4),
) -> CFDecodeOut:
    """Decode an Italian codice fiscale + report mismatches.

    Pure helper — does not touch the database. The frontend uses it
    on the patient edit form to surface inline warnings (e.g. typed
    birth_date disagrees with the CF the user just pasted), and the
    MCP agent can call it before issuing an ``update_patient`` to
    validate inputs without committing.
    """
    from bvphoenix.services.codice_fiscale import check_consistency, decode_codice_fiscale

    decoded = decode_codice_fiscale(cf)
    warnings = [
        {
            "field": w.field,
            "stored": w.stored,
            "decoded_from_cf": w.decoded_from_cf,
            "detail": w.detail,
        }
        for w in check_consistency(
            cf,
            birth_date=birth_date,
            sex=sex,
            birth_place_belfiore=birth_place_belfiore,
        )
    ]
    decoded_dict: dict | None = None
    if decoded is not None:
        decoded_dict = {
            "surname_initials": decoded.surname_initials,
            "first_name_initials": decoded.first_name_initials,
            "birth_date": decoded.birth_date.isoformat() if decoded.birth_date else None,
            "sex": decoded.sex,
            "birth_place_belfiore": decoded.birth_place_belfiore,
            "is_foreign_born": decoded.is_foreign_born,
        }
    return CFDecodeOut(
        cf=cf.strip().upper(),
        valid_syntax=decoded is not None,
        decoded=decoded_dict,
        warnings=warnings,
    )


@router.delete("/patients/{patient_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_patient(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> None:
    # Erasing a fascicolo is a destructive, legally significant action;
    # keep it human-only even if the agent token would otherwise satisfy
    # ``patient:write``. Counterpart of ``consultations:finalize``.
    if getattr(request.state, "is_agent", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agents cannot delete patients (human-only)",
        )
    patient = await _get_patient_or_404(db, patient_id, user, request, action=DELETE)
    await db.delete(patient)
    await db.commit()
    await audit.log(
        action="patient_delete",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient_id,
    )
