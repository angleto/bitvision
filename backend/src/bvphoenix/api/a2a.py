"""Agent-to-Agent (A2A) protocol support.

Implements the A2A v1.0 protocol for agent-to-agent communication:
- Agent Card at /.well-known/agent-card.json (discovery)
- JSON-RPC 2.0 task endpoint at /api/a2a (task lifecycle)

This enables external AI agents (e.g. a doctor's agent) to discover
bitvision's capabilities and delegate tasks like "find similar cases",
"analyze this study", or "generate a report".

Spec: https://a2a-protocol.org/latest/specification/
"""

from __future__ import annotations

import inspect
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import public_user
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditContext, AuditDep
from bvphoenix.services.a2a_intent import IntentResult, parse_intent
from bvphoenix.services.a2a_store import get_store
from bvphoenix.services.rate_limit import A2A_LIMIT, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["a2a"])

# ---- Agent Card (Discovery) ----

AGENT_CARD = {
    "name": "bitvision-phoenix",
    "description": (
        "Open-source medical imaging platform with DICOM archive, "
        "BiomedCLIP visual similarity search, LLM-powered analysis, "
        "and patient radiology records (fascicolo elettronico). "
        "Supports radiology consultation, case search, and report generation."
    ),
    "version": "0.1.0",
    "url": "/api/a2a",
    "supported_interfaces": [
        {
            "protocol_binding": "JSONRPC",
            "url": "/api/a2a",
        }
    ],
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": True,
    },
    "default_input_modes": ["text"],
    "default_output_modes": ["text"],
    "skills": [
        {
            "id": "dicom-search",
            "name": "Search DICOM studies",
            "description": (
                "Full-text and metadata search across DICOM studies. "
                "Filter by modality, body part, date range, and tags."
            ),
            "tags": ["search", "dicom", "radiology"],
            "examples": [
                "Find all chest CT studies from the last 6 months",
                "Search for brain MR studies with description containing 'tumor'",
            ],
        },
        {
            "id": "similarity-search",
            "name": "Visual similarity search",
            "description": (
                "Find visually similar medical images using BiomedCLIP "
                "embeddings. Provide a study or series ID as reference."
            ),
            "tags": ["similarity", "embeddings", "biomedclip", "visual"],
            "examples": [
                "Find cases visually similar to study abc-123",
                "Show me similar CT chest scans to this series",
            ],
        },
        {
            "id": "image-analysis",
            "name": "AI-assisted image analysis",
            "description": (
                "Generate LLM-powered clinical descriptions for DICOM series. "
                "Descriptions are saved as annotations for future reference."
            ),
            "tags": ["llm", "analysis", "description", "annotation"],
            "examples": [
                "Describe the findings in this chest CT series",
                "Generate a clinical description focusing on cardiac structures",
            ],
        },
        {
            "id": "patient-fascicolo",
            "name": "Patient radiology record",
            "description": (
                "Access a patient's complete radiology record (fascicolo). "
                "Includes demographics, studies, reports, clinical documents, "
                "annotations, and timeline view. Inspired by Italian FSE 2.0."
            ),
            "tags": ["patient", "fascicolo", "fse", "record", "timeline"],
            "examples": [
                "Get the complete imaging history for patient X",
                "Show me the fascicolo index with section counts",
            ],
        },
        {
            "id": "radiology-consultation",
            "name": "Radiology consultation",
            "description": (
                "Multi-step radiology consultation: search for relevant cases, "
                "analyze images, compare with similar findings, and produce "
                "a structured consultation response."
            ),
            "tags": ["consultation", "radiology", "multi-step"],
            "examples": [
                "I have a 65y/o patient with suspected pulmonary nodule, help me find similar cases and analyze",
            ],
        },
        {
            "id": "fascicolo-executive-summary",
            "name": "Fascicolo executive summary",
            "description": (
                "Riassumi il fascicolo completo di un paziente in 3-5 punti "
                "chiave, in italiano o inglese."
            ),
            "tags": ["summary", "fascicolo", "executive"],
            "examples": [
                "Riassumi il fascicolo del paziente X",
                "3-bullet summary for patient Y",
                "Provide an executive overview of this patient's imaging history",
            ],
        },
    ],
    "security_schemes": [
        {
            "type": "Bearer",
            "description": "JWT Bearer token from bitvision phoenix authentication",
        }
    ],
}


# ---- Task state ----


class TaskState:
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_task(message: dict, context_id: str | None = None) -> dict:
    task_id = str(uuid.uuid4())
    return {
        "id": task_id,
        "contextId": context_id or str(uuid.uuid4()),
        "status": {"state": TaskState.SUBMITTED, "timestamp": _now_iso()},
        "messages": [message] if message else [],
        "artifacts": [],
        "metadata": {},
    }


# ---- JSON-RPC 2.0 Endpoint ----


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _jsonrpc_result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


@router.post("/a2a")
@limiter.limit(A2A_LIMIT)
async def a2a_endpoint(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    audit: AuditDep,
) -> JSONResponse:
    """A2A JSON-RPC 2.0 endpoint.

    Supports methods:
    - agent/sendMessage: Create or continue a task
    - agent/getTask: Get task state
    - agent/listTasks: List tasks for a context
    - agent/cancelTask: Cancel a task
    - agent/getAgentCard: Get the agent card (authenticated variant)
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"))

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {})

    if not method:
        return JSONResponse(_jsonrpc_error(req_id, -32600, "Invalid Request: missing method"))

    store = get_store()

    if method == "agent/getAgentCard":
        return JSONResponse(_jsonrpc_result(req_id, AGENT_CARD))

    if method == "agent/sendMessage":
        return await _handle_send_message(req_id, params, db, user, audit)

    if method == "agent/getTask":
        task_id = params.get("taskId") or params.get("task_id")
        task = await store.get_task(task_id) if task_id else None
        if task is None:
            return JSONResponse(_jsonrpc_error(req_id, -32602, "Task not found"))
        return JSONResponse(_jsonrpc_result(req_id, task))

    if method == "agent/listTasks":
        context_id = params.get("contextId") or params.get("context_id")
        tasks = await store.list_tasks(context_id)
        return JSONResponse(_jsonrpc_result(req_id, {"tasks": tasks}))

    if method == "agent/cancelTask":
        task_id = params.get("taskId") or params.get("task_id")
        task = await store.get_task(task_id) if task_id else None
        if task is None:
            return JSONResponse(_jsonrpc_error(req_id, -32602, "Task not found"))
        task["status"] = {"state": TaskState.CANCELED, "timestamp": _now_iso()}
        await store.save_task(task_id, task)
        return JSONResponse(_jsonrpc_result(req_id, task))

    return JSONResponse(_jsonrpc_error(req_id, -32601, f"Method not found: {method}"))


async def _handle_send_message(
    req_id: Any,
    params: dict,
    db: AsyncSession,
    user: User | None,
    audit: AuditContext,
) -> JSONResponse:
    """Handle agent/sendMessage — create a task or extend an existing one."""
    store = get_store()
    message = params.get("message", {})
    task_id = params.get("taskId") or params.get("task_id")
    context_id = params.get("contextId") or params.get("context_id")

    task: dict | None = None
    if task_id:
        task = await store.get_task(task_id)

    is_new_task = task is None
    if task is None:
        task = _new_task(message, context_id)
    else:
        task["messages"].append(message)

    if is_new_task:
        await audit.log(
            action="a2a_task_created",
            actor_subject_id=user.subject_id if user else None,
            resource_kind="a2a_task",
            resource_id=uuid.UUID(task["id"]),
            metadata={"context_id": task.get("contextId")},
        )

    task["status"] = {"state": TaskState.WORKING, "timestamp": _now_iso()}

    user_text = _extract_text(message)
    if not user_text:
        task["status"] = {
            "state": TaskState.INPUT_REQUIRED,
            "timestamp": _now_iso(),
            "message": {
                "role": "agent",
                "parts": [
                    {
                        "type": "text",
                        "text": (
                            "Please describe what you need. I can search for DICOM studies, "
                            "find similar cases, analyze images, or access Health Records."
                        ),
                    }
                ],
            },
        }
        await store.save_task(task["id"], task)
        return JSONResponse(_jsonrpc_result(req_id, task))

    intent = await parse_intent(user_text)
    task.setdefault("metadata", {})["last_intent"] = intent.model_dump()

    try:
        await _execute_skill(task, intent, user_text, db, user)
    except HTTPException as exc:
        task["status"] = {
            "state": TaskState.FAILED,
            "timestamp": _now_iso(),
            "error": {"status": exc.status_code, "detail": str(exc.detail)},
        }
    except Exception as exc:
        task["status"] = {
            "state": TaskState.FAILED,
            "timestamp": _now_iso(),
            "error": {"detail": str(exc)},
        }

    await store.save_task(task["id"], task)
    if task.get("status", {}).get("state") == TaskState.COMPLETED:
        await audit.log(
            action="a2a_task_completed",
            actor_subject_id=user.subject_id if user else None,
            resource_kind="a2a_task",
            resource_id=uuid.UUID(task["id"]),
            metadata={
                "skill_id": task.get("metadata", {}).get("last_intent", {}).get("skill_id"),
            },
        )
    return JSONResponse(_jsonrpc_result(req_id, task))


def _extract_text(message: dict) -> str:
    parts = message.get("parts")
    if parts:
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        return " ".join(t for t in texts if t).strip()
    # Convenience: accept {"content": "..."} as a shorthand.
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _text_artifact(name: str, text: str) -> dict:
    return {"name": name, "parts": [{"type": "text", "text": text}]}


def _data_artifact(name: str, data: Any) -> dict:
    return {"name": name, "parts": [{"type": "data", "data": data}]}


# ---- Skill dispatch ----


async def _execute_skill(
    task: dict,
    intent: IntentResult,
    user_text: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    """Dispatch to the backend logic for ``intent.skill_id``."""
    # Lazy imports — keeps the module import graph clean and avoids
    # circulars when api/__init__ wires routers.
    skill = intent.skill_id
    if skill == "dicom-search":
        await _run_dicom_search(task, intent, user_text, db, user)
        return
    if skill == "similarity-search":
        await _run_similarity_search(task, intent, user_text, db, user)
        return
    if skill == "image-analysis":
        await _run_image_analysis(task, intent, user_text, db, user)
        return
    if skill == "patient-fascicolo":
        await _run_patient_fascicolo(task, intent, db, user)
        return
    if skill == "radiology-consultation":
        await _run_radiology_consultation(task, intent, user_text, db, user)
        return
    if skill == "fascicolo-executive-summary":
        await _run_fascicolo_executive_summary(task, intent, user_text, db, user)
        return

    task["status"] = {"state": TaskState.FAILED, "timestamp": _now_iso()}
    task["artifacts"].append(_text_artifact("error", f"Unknown skill: {skill}"))


def _detect_lang(intent: IntentResult, user_text: str) -> str:
    """Resolve the output language for an LLM call.

    Order of precedence:
    1. Explicit ``lang`` / ``language`` param in the intent.
    2. Simple keyword sniff on the user message.
    3. Default to Italian — the primary UI language.
    """
    explicit = intent.params.get("lang") or intent.params.get("language")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    lower = (user_text or "").lower()
    english_tokens = ("in english", "english summary", "reply in english", "3-bullet summary")
    if any(token in lower for token in english_tokens):
        return "en"
    italian_tokens = ("in italiano", "riassumi", "riassunto", "paziente")
    if any(token in lower for token in italian_tokens):
        return "it"
    return "it"


async def _run_dicom_search(
    task: dict,
    intent: IntentResult,
    user_text: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    from sqlalchemy import func, or_

    from bvphoenix.api._schemas import StudyOut
    from bvphoenix.db.models import ImagingStudy, Series
    from bvphoenix.services.permissions import visible_studies_filter

    query_text = intent.params.get("query") or user_text
    base = await visible_studies_filter(db, user)
    ts_query = func.plainto_tsquery("simple", query_text)
    q = (
        base.outerjoin(Series, Series.study_id == ImagingStudy.id)
        .where(
            or_(
                func.to_tsvector("simple", func.coalesce(ImagingStudy.study_description, "")).op(
                    "@@"
                )(ts_query),
                func.to_tsvector("simple", func.coalesce(Series.series_description, "")).op("@@")(
                    ts_query
                ),
            )
        )
        .distinct()
        .limit(10)
    )
    rows = (await db.execute(q)).scalars().unique().all()
    studies = [StudyOut.model_validate(r).model_dump(mode="json") for r in rows]

    if not studies:
        summary = f"No studies found matching '{query_text}'."
    else:
        lines = [f"Found {len(studies)} study(ies):"]
        for s in studies:
            lines.append(
                f"- {s.get('study_description') or '(no description)'} | "
                f"modalities: {','.join(s.get('modalities') or [])} | "
                f"date: {s.get('study_date') or 'unknown'} | id: {s.get('id')}"
            )
        summary = "\n".join(lines)

    task["status"] = {"state": TaskState.COMPLETED, "timestamp": _now_iso()}
    task["artifacts"] = [
        _text_artifact("response", summary),
        _data_artifact("studies", studies),
    ]


async def _run_similarity_search(
    task: dict,
    intent: IntentResult,
    user_text: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    from bvphoenix.api.search import find_similar_studies

    target_id = intent.params.get("target_id")
    if not target_id:
        task["status"] = _input_required_status(
            "To find similar cases, I need a study or series UUID. "
            "Reply with the id (e.g. 'similar to <uuid>')."
        )
        return

    try:
        target_uuid = uuid.UUID(str(target_id))
    except (ValueError, TypeError):
        task["status"] = _input_required_status(
            f"'{target_id}' is not a valid UUID. Please provide a study or series id."
        )
        return

    k = int(intent.params.get("k") or 10)
    modality = intent.params.get("modality")

    results = await find_similar_studies(
        db=db, user=user, target_id=target_uuid, k=k, modality=modality
    )
    payload = [r.model_dump(mode="json") for r in results]

    if not payload:
        summary = f"No similar cases found for {target_uuid}."
    else:
        lines = [f"Found {len(payload)} similar case(s) for {target_uuid}:"]
        for entry in payload:
            study = entry.get("study", {})
            lines.append(
                f"- score={entry.get('score'):.3f} "
                f"id={study.get('id')} "
                f"desc={study.get('study_description') or '(no description)'} "
                f"modalities={','.join(study.get('modalities') or [])}"
            )
        summary = "\n".join(lines)

    task["status"] = {"state": TaskState.COMPLETED, "timestamp": _now_iso()}
    task["artifacts"] = [
        _text_artifact("response", summary),
        _data_artifact("similar_studies", payload),
    ]


async def _run_image_analysis(
    task: dict,
    intent: IntentResult,
    user_text: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    from sqlalchemy import select

    from bvphoenix.db.models import ImagingStudy, Series
    from bvphoenix.services.llm import get_llm_provider

    series_id = intent.params.get("series_id") or intent.params.get("target_id")
    if not series_id:
        task["status"] = _input_required_status(
            "To analyze an image, I need a series UUID. Reply with the series id and optional hint."
        )
        return

    try:
        series_uuid = uuid.UUID(str(series_id))
    except (ValueError, TypeError):
        task["status"] = _input_required_status(
            f"'{series_id}' is not a valid UUID. Please provide a series id."
        )
        return

    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_uuid)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"series {series_uuid} not found")
    series, _study = row

    hint = intent.params.get("hint") or user_text
    lang = _detect_lang(intent, user_text)
    provider = get_llm_provider()
    result = await provider.describe_series(
        modality=series.modality,
        body_part=series.body_part_examined,
        hint=hint,
        lang=lang,
    )

    task["status"] = {"state": TaskState.COMPLETED, "timestamp": _now_iso()}
    task["artifacts"] = [
        _text_artifact("response", result.text),
        _data_artifact(
            "description",
            {
                "series_id": str(series_uuid),
                "text": result.text,
                "model_id": result.model_id,
                "confidence": result.confidence,
            },
        ),
    ]


async def _run_patient_fascicolo(
    task: dict,
    intent: IntentResult,
    db: AsyncSession,
    user: User | None,
) -> None:
    from bvphoenix.api.patients import get_fascicolo_index, get_timeline

    patient_id = intent.params.get("patient_id")
    if not patient_id:
        task["status"] = _input_required_status("Which patient? Please provide a patient UUID.")
        return

    try:
        patient_uuid = uuid.UUID(str(patient_id))
    except (ValueError, TypeError):
        task["status"] = _input_required_status(
            f"'{patient_id}' is not a valid UUID. Please provide a patient id."
        )
        return

    index = await get_fascicolo_index(patient_id=patient_uuid, db=db, user=user)
    section = intent.params.get("section")
    timeline = await get_timeline(
        patient_id=patient_uuid, db=db, user=user, section=section, limit=50, offset=0
    )

    index_payload = index.model_dump(mode="json")
    timeline_payload = [t.model_dump(mode="json") for t in timeline]

    summary_lines = [f"Fascicolo for patient {patient_uuid}:"]
    for s in index_payload.get("sections", []):
        summary_lines.append(f"- {s['label']}: {s['count']} item(s)")
    summary_lines.append(f"Timeline: {len(timeline_payload)} entries (most recent first).")

    task["status"] = {"state": TaskState.COMPLETED, "timestamp": _now_iso()}
    task["artifacts"] = [
        _text_artifact("response", "\n".join(summary_lines)),
        _data_artifact("fascicolo_index", index_payload),
        _data_artifact("timeline", timeline_payload),
    ]


async def _run_radiology_consultation(
    task: dict,
    intent: IntentResult,
    user_text: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    """Minimal state machine: ask for a study_id, then run similarity + analysis."""
    study_id = intent.params.get("study_id") or intent.params.get("target_id")

    # The conversation history may already contain a study id. Scan it.
    if not study_id:
        for msg in task.get("messages", []):
            text = _extract_text(msg)
            from bvphoenix.services.a2a_intent import _extract_uuid

            found = _extract_uuid(text)
            if found:
                study_id = found
                break

    if not study_id:
        task["status"] = _input_required_status(
            "To start the consultation, please provide a study UUID so I can "
            "find similar cases and summarize the imaging findings."
        )
        return

    try:
        study_uuid = uuid.UUID(str(study_id))
    except (ValueError, TypeError):
        task["status"] = _input_required_status(
            f"'{study_id}' is not a valid UUID. Please provide a study id."
        )
        return

    # Step 1: similarity search
    from sqlalchemy import select

    from bvphoenix.api.search import find_similar_studies
    from bvphoenix.db.models import ImagingStudy, Series

    similar: list = []
    try:
        similar = await find_similar_studies(
            db=db, user=user, target_id=study_uuid, k=5, modality=None
        )
    except HTTPException:
        similar = []
    similar_payload = [s.model_dump(mode="json") for s in similar]

    # Step 2: pick a series on the study and describe it
    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.study_id == study_uuid)
            .limit(1)
        )
    ).first()

    description_payload: dict | None = None
    if row is not None:
        series, _study = row
        from bvphoenix.services.llm import get_llm_provider

        provider = get_llm_provider()
        lang = _detect_lang(intent, user_text)
        result = await provider.describe_series(
            modality=series.modality,
            body_part=series.body_part_examined,
            hint=user_text,
            lang=lang,
        )
        description_payload = {
            "series_id": str(series.id),
            "text": result.text,
            "model_id": result.model_id,
            "confidence": result.confidence,
        }

    summary_lines = [f"Consultation for study {study_uuid}:"]
    if description_payload:
        summary_lines.append(f"Imaging summary: {description_payload['text']}")
    else:
        summary_lines.append("No series available for automated description.")
    summary_lines.append(f"Similar cases: {len(similar_payload)}")
    for entry in similar_payload[:3]:
        study = entry.get("study", {})
        summary_lines.append(
            f"  - score={entry.get('score'):.3f} id={study.get('id')} "
            f"desc={study.get('study_description') or '(no description)'}"
        )

    title = f"Radiology consultation for study {study_uuid}"
    summary_md = "\n".join(summary_lines)

    findings_parts: list[str] = []
    if description_payload:
        findings_parts.append("## Imaging description")
        findings_parts.append(description_payload["text"])
    else:
        findings_parts.append(
            "## Imaging description\n_No series available for automated description._"
        )
    if similar_payload:
        findings_parts.append("\n## Similar prior cases")
        for entry in similar_payload[:5]:
            study = entry.get("study", {})
            findings_parts.append(
                f"- score={entry.get('score'):.3f} id={study.get('id')} "
                f"desc={study.get('study_description') or '(no description)'}"
            )
    findings_md = "\n".join(findings_parts)

    recommendations_md = (
        "## Recommendations\n"
        "- Correlate the automated imaging description with clinical history.\n"
        "- Review visually similar cases for comparable findings and management.\n"
        "- This is an AI-assisted consultation; a qualified radiologist must "
        "confirm findings before clinical action."
    )

    citations = [
        {
            "study": {"id": str(study_uuid)},
            "similar_studies": [
                {
                    "study_id": (entry.get("study") or {}).get("id"),
                    "score": entry.get("score"),
                }
                for entry in similar_payload
            ],
        }
    ]

    from bvphoenix.config import get_settings

    settings = get_settings()
    provider_name = settings.llm_provider or "stub"
    model_id = (
        (description_payload or {}).get("model_id") or settings.llm_default_model or "unknown"
    )

    consent_snapshot: dict = {}
    try:
        from bvphoenix.services.consent_snapshot import build_consent_snapshot
    except ImportError:
        build_consent_snapshot = None  # type: ignore[assignment]
    if build_consent_snapshot is not None:
        try:
            result = build_consent_snapshot(user=user, study_id=study_uuid)
            consent_snapshot = await result if inspect.isawaitable(result) else result
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("build_consent_snapshot failed: %s", exc)

    author_subject_id = user.subject_id if user is not None else None

    consultation_payload = {
        "title": title,
        "summary_md": summary_md,
        "findings_md": findings_md,
        "recommendations_md": recommendations_md,
        "citations": citations,
        "author_kind": "agent",
        "author_subject_id": author_subject_id,
        "model_id": model_id,
        "provider": provider_name,
        "deidentified_input": True,
        "consent_snapshot": consent_snapshot,
        "study_id": str(study_uuid),
    }

    consultation_id: str | None = None
    consultation_url: str | None = None
    try:
        try:
            from bvphoenix.services.consultations import (  # type: ignore
                create_consultation_from_payload,
            )
        except ImportError:
            create_consultation_from_payload = None  # type: ignore[assignment]

        if create_consultation_from_payload is not None:
            created = await create_consultation_from_payload(
                db=db, payload=consultation_payload, user=user
            )
            cid = getattr(created, "id", None) or (
                created.get("id") if isinstance(created, dict) else None
            )
            if cid is not None:
                consultation_id = str(cid)
        else:
            try:
                from bvphoenix.db.models import (  # type: ignore
                    Consultation,
                    ConsultationCitation,
                )
            except ImportError:
                Consultation = None  # type: ignore[assignment]
                ConsultationCitation = None  # type: ignore[assignment]

            if Consultation is not None:
                row = Consultation(
                    title=title,
                    summary_md=summary_md,
                    findings_md=findings_md,
                    recommendations_md=recommendations_md,
                    author_kind="agent",
                    author_subject_id=author_subject_id,
                    model_id=model_id,
                    provider=provider_name,
                    deidentified_input=True,
                    consent_snapshot=consent_snapshot,
                    study_id=study_uuid,
                )
                db.add(row)
                await db.flush()
                consultation_id = str(row.id)
                if ConsultationCitation is not None:
                    for cit in citations:
                        db.add(
                            ConsultationCitation(
                                consultation_id=row.id,
                                payload=cit,
                            )
                        )
                await db.commit()
            else:
                # TODO(C1/C2): Consultation model not yet landed. Skipping
                # persistence — coordinator gates the final merge on C1-C3.
                logger.info(
                    "Consultation model unavailable; skipping persistence "
                    "for a2a radiology-consultation task %s",
                    task.get("id"),
                )
    except Exception as exc:
        logger.warning(
            "Consultation persistence failed for task %s: %s",
            task.get("id"),
            exc,
        )
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - rollback best-effort
            pass
        consultation_id = None

    if consultation_id is not None:
        consultation_url = f"/api/consultations/{consultation_id}"

    task["status"] = {"state": TaskState.COMPLETED, "timestamp": _now_iso()}
    artifacts: list[dict] = [
        _text_artifact("response", "\n".join(summary_lines)),
        _data_artifact(
            "consultation",
            {
                "study_id": str(study_uuid),
                "description": description_payload,
                "similar_studies": similar_payload,
                "title": title,
                "summary_md": summary_md,
                "findings_md": findings_md,
                "recommendations_md": recommendations_md,
                "citations": citations,
            },
        ),
    ]
    if consultation_id is not None:
        artifacts.append(
            _data_artifact(
                "consultation_ref",
                {"consultation_id": consultation_id, "url": consultation_url},
            )
        )
    task["artifacts"] = artifacts


async def _run_fascicolo_executive_summary(
    task: dict,
    intent: IntentResult,
    user_text: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    """Produce a 3-5 bullet executive summary of a patient's fascicolo.

    Loads the same bundle surfaced by ``/api/patients/{id}/index`` and
    ``/api/patients/{id}/timeline`` plus the standalone documents, then
    asks the configured LLM provider to distill main findings, trends,
    and gaps. Falls back through the stub provider when no API key is
    configured so CI exercises the plumbing end-to-end.
    """
    from bvphoenix.api.patients import (
        get_fascicolo_index,
        get_timeline,
        list_documents,
    )
    from bvphoenix.services.a2a_intent import _extract_uuid
    from bvphoenix.services.llm import get_llm_provider

    patient_id = intent.params.get("patient_id")
    if not patient_id:
        # Multi-turn: caller may have supplied the id in an earlier message.
        for msg in task.get("messages", []):
            text = _extract_text(msg)
            found = _extract_uuid(text)
            if found:
                patient_id = found
                break

    if not patient_id:
        task["status"] = _input_required_status(
            "Which patient? Please provide a patient UUID so I can summarize the fascicolo."
        )
        return

    try:
        patient_uuid = uuid.UUID(str(patient_id))
    except (ValueError, TypeError):
        task["status"] = _input_required_status(
            f"'{patient_id}' is not a valid UUID. Please provide a patient id."
        )
        return

    index = await get_fascicolo_index(patient_id=patient_uuid, db=db, user=user)
    timeline = await get_timeline(
        patient_id=patient_uuid,
        db=db,
        user=user,
        section=None,
        limit=50,
        offset=0,
    )
    documents = await list_documents(patient_id=patient_uuid, db=db, user=user, type=None)

    index_payload = index.model_dump(mode="json")
    timeline_payload = [t.model_dump(mode="json") for t in timeline]
    documents_payload = [d.model_dump(mode="json") for d in documents]

    bundle = {
        "index": index_payload,
        "timeline": timeline_payload,
        "documents": documents_payload,
    }
    patient_label = index_payload.get("patient", {}).get("display_name") or str(patient_uuid)

    lang = _detect_lang(intent, user_text)
    provider = get_llm_provider()
    result = await provider.summarize_fascicolo(
        patient_label=patient_label,
        bundle=bundle,
        lang=lang,
    )

    bullets = [
        line.lstrip("-* ").strip()
        for line in result.text.splitlines()
        if line.strip().startswith(("-", "*"))
    ]
    if not bullets:
        bullets = [result.text.strip()]

    task["status"] = {"state": TaskState.COMPLETED, "timestamp": _now_iso()}
    task["artifacts"] = [
        _text_artifact("response", result.text),
        _data_artifact(
            "summary",
            {
                "patient_id": str(patient_uuid),
                "patient_label": patient_label,
                "lang": lang,
                "bullets": bullets,
                "text": result.text,
                "model_id": result.model_id,
                "summary_id": None,
            },
        ),
    ]


def _input_required_status(prompt: str) -> dict:
    return {
        "state": TaskState.INPUT_REQUIRED,
        "timestamp": _now_iso(),
        "message": {
            "role": "agent",
            "parts": [{"type": "text", "text": prompt}],
        },
    }
