"""LLM classifier that proposes care phases for a patient's timeline.

Pipeline:

1. Fetch every ``ClinicalEvent`` of the patient (cross-patient is
   impossible by construction: the SQL filter is the patient_id of the
   route).
2. Compute an ``input_hash`` from the events; if a recent
   ``CarePhaseProposal`` row with the same hash exists, reuse it
   without invoking the LLM (cache).
3. Build the prompt (system + user) and call the configured
   ``LLMProvider`` (``StubLLM`` in dev, ``AnthropicLLM`` in prod).
4. Parse the JSON response, validate against
   :class:`ClassifierOutput`. If validation fails, optionally run a
   second pass through a verifier LLM that receives the failed draft
   plus a checklist (chronology, gap detection, kind coherence).
5. Persist the proposal as a ``CarePhaseProposal`` row.

The classifier never writes to ``CarePhase`` or ``ClinicalEvent``
directly. The downstream ``apply_phase_proposal`` endpoint is the
single write path that materialises the proposal into the data model;
that step is gated by the ``phases:write`` scope and audit-logged.

Provenance: the proposal row records ``model_id`` and stores the raw
``payload`` JSONB, so an auditor can reconstruct what the model said
versus what the human accepted (memory ``ai_provenance``).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import CarePhaseProposal, ClinicalEvent
from bvphoenix.services.audit import log_action
from bvphoenix.services.care_phase_classifier_schema import (
    JSON_SCHEMA_FOR_PROMPT,
    ClassifierOutput,
)
from bvphoenix.services.care_phase_schemas import ProposalPayload, ProposePhasesOut
from bvphoenix.services.care_phases import compute_input_hash
from bvphoenix.services.llm import LLMProvider, StubLLM, get_llm_provider

_log = logging.getLogger(__name__)

# Reuse a cached proposal if its hash matches and it is fresher than
# this many days. Avoids re-invoking the LLM when the events have not
# changed and an agent re-asks for proposals shortly after.
_CACHE_TTL = timedelta(days=7)


# ----------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------


_SYSTEM_PROMPT_IT = """\
Sei un assistente clinico esperto che organizza la timeline di un singolo paziente \
in fasi cliniche semantiche (es. "Imaging pre-operatorio", "Intervento chirurgico", \
"Follow-up post-operatorio", "Sorveglianza", "Rivalutazione"). Riceverai SOLO eventi \
di un paziente per volta. Non riferirti mai a dati di altri pazienti.

Compito: leggere l'elenco cronologico di eventi clinici del paziente e produrre un \
JSON valido secondo lo schema fornito. Regole imprescindibili:

1. Le fasi devono coprire l'intero arco temporale degli eventi: ogni evento deve essere \
   assegnato a una fase. Nessun evento può rimanere senza fase.
2. Le fasi devono essere ordinate cronologicamente (ordinal=0 la più antica).
3. Lo slug deve essere stabile, in italiano kebab-case (es. "intervento-chirurgico"), \
   senza accenti e senza spazi.
4. Una fase aggrega eventi semanticamente coerenti: l'imaging pre-operatorio non si \
   mescola con l'intervento; il follow-up post-operatorio non si mescola con la \
   sorveglianza periodica successiva.
5. Il name_i18n deve avere SEMPRE le chiavi "it" e "en" valorizzate.
6. La confidence di ogni assegnazione esprime quanto sei sicuro che quell'evento \
   appartenga a quella fase (0.0..1.0).
7. Non inventare event_id: usa esclusivamente quelli forniti nell'input.
8. Output: SOLO il JSON, niente testo prima o dopo, niente code-fence.
"""


_SYSTEM_PROMPT_EN = """\
You are an expert clinical assistant that organises a single patient's timeline \
into semantic care phases (e.g. "Pre-operative imaging", "Surgical procedure", \
"Post-operative follow-up", "Surveillance", "Reassessment"). You will receive ONLY \
the events of one patient at a time. Never reference data of other patients.

Task: read the patient's chronological event list and produce valid JSON matching \
the provided schema. Mandatory rules:

1. Phases must cover the full time span of the events: every event must be assigned \
   to a phase. No event may remain unassigned.
2. Phases must be chronologically ordered (ordinal=0 is the earliest).
3. The slug must be stable Italian kebab-case (e.g. "intervento-chirurgico"), no \
   accents, no spaces.
4. A phase aggregates semantically coherent events: pre-operative imaging does not \
   mix with the surgery itself; post-op follow-up does not mix with later \
   periodic surveillance.
5. name_i18n MUST always carry both "it" and "en" keys.
6. The confidence of each assignment expresses how sure you are about that event \
   belonging to that phase (0.0..1.0).
7. Do not invent event_id: use only those from the input.
8. Output: ONLY the JSON, no text before or after, no code fence.
"""


def _system_prompt(lang: str) -> str:
    return _SYSTEM_PROMPT_IT if lang == "it" else _SYSTEM_PROMPT_EN


def _build_user_prompt(events: Sequence[ClinicalEvent], *, lang: str) -> str:
    """Render the patient's events as a JSON payload + the schema."""
    events_json = [
        {
            "id": str(e.id),
            "kind": e.kind,
            "event_date": e.event_date.isoformat() if e.event_date else None,
            "title": e.title,
            "body_part": e.body_part,
            "narrative": (e.narrative or "")[:1000],
        }
        for e in events
    ]
    schema_text = json.dumps(JSON_SCHEMA_FOR_PROMPT, indent=2)
    events_text = json.dumps(events_json, indent=2, ensure_ascii=False)
    if lang == "it":
        return (
            "EVENTI DEL PAZIENTE (elenco completo, cronologico):\n\n"
            f"{events_text}\n\n"
            "SCHEMA JSON ATTESO:\n\n"
            f"{schema_text}\n\n"
            "Produci ora il JSON di risposta."
        )
    return (
        "PATIENT EVENTS (full chronological list):\n\n"
        f"{events_text}\n\n"
        "EXPECTED JSON SCHEMA:\n\n"
        f"{schema_text}\n\n"
        "Now produce the JSON response."
    )


_VERIFIER_PROMPT_IT = """\
Sei un verificatore. Hai prodotto la seguente proposta di fasi cliniche per un \
paziente. Controlla se rispetta TUTTE queste regole:

- Ogni evento è assegnato a una fase.
- Nessun event_id inventato.
- Slug unici e in kebab-case italiano.
- Fasi ordinate cronologicamente (per evento più antico contenuto).
- Coerenza semantica: l'imaging pre-operatorio non si mescola con l'intervento, \
  il follow-up post-op non si mescola con la sorveglianza periodica successiva.
- name_i18n con chiavi "it" e "en" valorizzate.

Se trovi errori, RIPRODUCI il JSON corretto secondo lo schema. Se è già corretto, \
ripeti il JSON identico. Output: SOLO il JSON.
"""


def _build_verifier_prompt(draft_text: str, *, lang: str) -> str:
    # Only the Italian verifier prompt exists today. The lang parameter
    # is kept on the signature so the call sites do not change when an
    # English variant is added.
    del lang
    return f"{_VERIFIER_PROMPT_IT}\n\nBOZZA DA VERIFICARE:\n\n{draft_text}\n"


# ----------------------------------------------------------------------
# Output parsing
# ----------------------------------------------------------------------


_JSON_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    """Tolerate accidental ```json fences from the model."""
    m = _JSON_FENCE.match(text.strip())
    if m:
        return m.group(1)
    return text


def parse_classifier_output(raw_text: str) -> ClassifierOutput:
    """Parse the LLM string output into a validated ``ClassifierOutput``.

    Raises ``ValueError`` on malformed JSON or schema violation so the
    caller can decide whether to escalate to the verifier pass.
    """
    cleaned = _strip_fence(raw_text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"classifier returned non-JSON: {exc}") from exc
    return ClassifierOutput.model_validate(data)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


async def propose_for_patient(
    *,
    patient_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    lang: str = "it",
    db: AsyncSession,
    request: Request | None = None,
    provider: LLMProvider | None = None,
    use_verifier: bool = True,
    bypass_cache: bool = False,
) -> ProposePhasesOut:
    """Generate a phase proposal for ``patient_id``.

    Returns the persisted proposal regardless of whether it was a cache
    hit or a fresh LLM call. The caller can then walk
    ``payload.phases`` / ``payload.assignments`` and ask the user to
    accept all or part of them via ``apply-proposal``.
    """
    events = (
        (
            await db.execute(
                select(ClinicalEvent)
                .where(ClinicalEvent.patient_id == patient_id)
                .order_by(
                    ClinicalEvent.event_date.asc().nulls_last(),
                    ClinicalEvent.created_at.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    if not events:
        raise HTTPException(
            status_code=409,
            detail="patient has no clinical events to classify",
        )

    input_hash = compute_input_hash(events)

    if not bypass_cache:
        cached = await _lookup_cached_proposal(db, patient_id=patient_id, input_hash=input_hash)
        if cached is not None:
            return _proposal_to_out(cached, cached_flag=True)

    provider = provider or get_llm_provider()

    # No embedded LLM provider is configured in this deployment
    # (BYO mode: users classify via their own MCP agent calling
    # ``propose_care_phases``). The stub echoes ``[stub] system=...``,
    # which is not JSON and would surface as the cryptic 502
    # ``Expecting value: line 1 column 2 (char 1)`` (the ``s`` of
    # ``[stub]``). Short-circuit with a 503 that matches the
    # ``llm_classifier=false`` contract on ``/api/system/features``;
    # the FE intercepts the status and renders a localized hint
    # pointing the user to the MCP path.
    if isinstance(provider, StubLLM):
        raise HTTPException(
            status_code=503,
            detail=(
                "embedded LLM classifier not configured in this deployment; "
                "use the MCP tool propose_care_phases via your AI assistant"
            ),
            headers={"X-Feature": "llm_classifier", "X-Feature-State": "byo-only"},
        )

    system = _system_prompt(lang)
    user = _build_user_prompt(events, lang=lang)
    result = await provider.complete(
        system=system,
        messages=[{"role": "user", "content": user}],
        cache_control=True,
        max_tokens=4096,
    )
    raw_text = result.text or ""

    try:
        parsed = parse_classifier_output(raw_text)
    except ValueError as draft_exc:
        if not use_verifier:
            raise HTTPException(
                status_code=502,
                detail=f"classifier returned invalid JSON: {draft_exc}",
            ) from draft_exc
        # Second-stage verifier pass.
        verifier = await provider.complete(
            system=system,
            messages=[{"role": "user", "content": _build_verifier_prompt(raw_text, lang=lang)}],
            max_tokens=4096,
        )
        try:
            parsed = parse_classifier_output(verifier.text or "")
        except ValueError as verifier_exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "classifier and verifier both returned invalid JSON: "
                    f"draft={draft_exc!s}; verifier={verifier_exc!s}"
                ),
            ) from verifier_exc
        raw_text = verifier.text or raw_text
        result = verifier  # propagate model_id of the last call

    # Cross-validation: every assignment.event_id must be one of the
    # input event ids. Defence in depth on the LLM output.
    valid_ids = {e.id for e in events}
    bad = [str(a.event_id) for a in parsed.assignments if a.event_id not in valid_ids]
    if bad:
        raise HTTPException(
            status_code=502,
            detail=(
                "classifier hallucinated event ids not present in the input: " + ", ".join(bad[:5])
            ),
        )

    payload_dict = parsed.model_dump(mode="json")

    proposal = CarePhaseProposal(
        patient_id=patient_id,
        job_id=None,
        payload=payload_dict,
        model_id=result.model_id,
        input_hash=input_hash,
        applied_at=None,
        applied_by_user_id=None,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)

    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_propose",
        resource_kind="care_phase_proposal",
        resource_id=proposal.id,
        request=request,
        metadata={
            "patient_id": str(patient_id),
            "n_events": len(events),
            "n_phases_proposed": len(parsed.phases),
            "model_id": result.model_id,
            "used_verifier": use_verifier,
            "input_hash": input_hash,
        },
    )

    return _proposal_to_out(proposal, cached_flag=False)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _lookup_cached_proposal(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    input_hash: str,
) -> CarePhaseProposal | None:
    cutoff = datetime.now(UTC) - _CACHE_TTL
    return (
        await db.execute(
            select(CarePhaseProposal)
            .where(
                and_(
                    CarePhaseProposal.patient_id == patient_id,
                    CarePhaseProposal.input_hash == input_hash,
                    CarePhaseProposal.created_at >= cutoff,
                )
            )
            .order_by(CarePhaseProposal.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _proposal_to_out(proposal: CarePhaseProposal, *, cached_flag: bool) -> ProposePhasesOut:
    payload = ProposalPayload.model_validate(proposal.payload)
    return ProposePhasesOut(
        proposal_id=proposal.id,
        job_id=proposal.job_id,
        status="cached" if cached_flag else "fresh",
        payload=payload,
        model_id=proposal.model_id,
        cached=cached_flag,
        created_at=proposal.created_at,
    )


__all__ = [
    "parse_classifier_output",
    "propose_for_patient",
]
