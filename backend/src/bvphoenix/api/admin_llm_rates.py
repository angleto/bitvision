"""Admin LLM rate-card management.

Read + write the ``llm_rate_cards`` table, refreshing the runtime
override cache on every mutation so a price change takes effect on the
very next debit call.

All endpoints are gated by :func:`require_admin`. List is read-only;
PATCH/POST upserts a row, DELETE drops one. The audit dependency logs
every write so an auditor can reconstruct who changed which rate when.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_admin
from bvphoenix.config import get_settings
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services import llm_rate_cards as svc
from bvphoenix.services.ai_tiers import AiTier, config_for_tier

router = APIRouter(tags=["admin-llm-rates"], prefix="/admin/llm-rates")


class RateCardSchema(BaseModel):
    model_id: str
    provider: str
    display_name: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    cache_creation_usd_per_mtok: float
    markup_pct: float | None
    tier_hint: str
    is_active: bool
    is_in_house: bool
    notes: str | None
    updated_at: datetime
    updated_by_subject_id: uuid.UUID | None

    @classmethod
    def from_dataclass(cls, dc: svc.RateCardOut) -> RateCardSchema:
        return cls(
            model_id=dc.model_id,
            provider=dc.provider,
            display_name=dc.display_name,
            input_usd_per_mtok=dc.input_usd_per_mtok,
            output_usd_per_mtok=dc.output_usd_per_mtok,
            cache_read_usd_per_mtok=dc.cache_read_usd_per_mtok,
            cache_creation_usd_per_mtok=dc.cache_creation_usd_per_mtok,
            markup_pct=dc.markup_pct,
            tier_hint=dc.tier_hint,
            is_active=dc.is_active,
            is_in_house=dc.is_in_house,
            notes=dc.notes,
            updated_at=dc.updated_at,
            updated_by_subject_id=dc.updated_by_subject_id,
        )


class RateCardUpsertIn(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    cache_read_usd_per_mtok: float = Field(default=0, ge=0)
    cache_creation_usd_per_mtok: float = Field(default=0, ge=0)
    markup_pct: float | None = Field(default=None, ge=0, le=500)
    tier_hint: str = Field(default="standard")
    is_active: bool = True
    is_in_house: bool = False
    notes: str | None = Field(default=None, max_length=512)


@router.get("", response_model=list[RateCardSchema])
async def list_all(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    only_active: bool = False,
) -> list[RateCardSchema]:
    rows = await svc.list_rate_cards(db, only_active=only_active)
    return [RateCardSchema.from_dataclass(r) for r in rows]


@router.put(
    "/{model_id}",
    response_model=RateCardSchema,
    status_code=status.HTTP_200_OK,
)
async def upsert_rate(
    model_id: str,
    body: RateCardUpsertIn,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> RateCardSchema:
    if body.model_id != model_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "model_id_mismatch",
                "path": model_id,
                "body": body.model_id,
            },
        )
    try:
        row = await svc.upsert_rate_card(
            db,
            actor_subject_id=admin.subject_id,
            model_id=body.model_id,
            provider=body.provider,
            display_name=body.display_name,
            input_usd_per_mtok=body.input_usd_per_mtok,
            output_usd_per_mtok=body.output_usd_per_mtok,
            cache_read_usd_per_mtok=body.cache_read_usd_per_mtok,
            cache_creation_usd_per_mtok=body.cache_creation_usd_per_mtok,
            markup_pct=body.markup_pct,
            tier_hint=body.tier_hint,
            is_active=body.is_active,
            is_in_house=body.is_in_house,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid", "message": str(exc)}
        ) from exc

    await audit.log(
        action="llm_rate_card.upsert",
        actor_subject_id=admin.subject_id,
        resource_kind="llm_rate_card",
        resource_id=None,
        metadata={
            "model_id": row.model_id,
            "provider": row.provider,
            "in": float(row.input_usd_per_mtok),
            "out": float(row.output_usd_per_mtok),
            "markup_pct": float(row.markup_pct) if row.markup_pct is not None else None,
            "is_active": row.is_active,
            "is_in_house": row.is_in_house,
        },
    )
    return RateCardSchema.from_dataclass(svc.RateCardOut.from_row(row))


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rate(
    model_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> None:
    deleted = await svc.delete_rate_card(db, model_id=model_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    await audit.log(
        action="llm_rate_card.delete",
        actor_subject_id=admin.subject_id,
        resource_kind="llm_rate_card",
        resource_id=None,
        metadata={"model_id": model_id},
    )


@router.post("/refresh", status_code=status.HTTP_200_OK)
async def refresh_now(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Force the runtime override cache to re-read from DB. Useful when
    a row was updated out-of-band (e.g. via direct SQL during an
    incident) and the operator wants the change picked up immediately."""
    n = await svc.refresh_rate_cards(db)
    return {"active_rows_loaded": n}


# ---------------------------------------------------------------------------
# Provider status + active tier defaults — operator visibility
# ---------------------------------------------------------------------------


class ProviderStatusOut(BaseModel):
    name: str
    configured: bool
    description: str
    note: str | None = None


class TierDefaultOut(BaseModel):
    tier: str
    provider_kind: str
    model_id: str
    is_callable: bool


class ProviderStatusBundle(BaseModel):
    providers: list[ProviderStatusOut]
    tier_defaults: list[TierDefaultOut]


@router.get("/provider-status", response_model=ProviderStatusBundle)
async def get_provider_status(
    _admin: Annotated[User, Depends(require_admin)],
) -> ProviderStatusBundle:
    """Snapshot of which LLM providers are usable right now and which
    model the tier resolver actually picks for each tier.

    The admin UI consumes this to:
    - render a "configured / not configured" badge per provider so a
      row in the rate-card table whose provider has no API key shows
      as "visible but not callable";
    - show the operator which model is currently the default for
      ``standard`` and ``premium`` calls (the tier resolver is the
      single source of truth — never derive the default from the
      ``tier_hint`` column on the rate card).

    The "configured" check is a presence test on the env-backed
    ``Settings`` field — it does not reach out to the provider's API.
    A misconfigured key will surface at call time.
    """
    s = get_settings()

    # Which providers have a credential set in the environment.
    provider_rows: list[ProviderStatusOut] = [
        ProviderStatusOut(
            name="anthropic",
            configured=bool(s.anthropic_api_key),
            description="Anthropic Messages API (Claude). USD-native; cache pricing supported.",
            note=None
            if s.anthropic_api_key
            else "Imposta BVP_ANTHROPIC_API_KEY nel cluster per attivare i modelli Claude.",
        ),
        ProviderStatusOut(
            name="openai",
            configured=bool(s.openai_api_key),
            description="OpenAI Chat Completions (gpt-4o, gpt-4o-mini). Anche usato come adapter per Scaleway.",
            note=None
            if s.openai_api_key
            else "Imposta BVP_OPENAI_API_KEY nel cluster per attivare i modelli OpenAI.",
        ),
        ProviderStatusOut(
            name="scaleway",
            configured=bool(s.scaleway_api_key),
            description="Scaleway Generative API (Mistral, Qwen, Gemma, Llama). EUR-native, fr-par.",
            note=None
            if s.scaleway_api_key
            else "Imposta BVP_SCALEWAY_API_KEY nel cluster per attivare i modelli Scaleway.",
        ),
        ProviderStatusOut(
            name="gemini",
            configured=False,
            description="Google Gemini API.",
            note="Adapter non ancora implementato. Le righe Gemini nella tabella sono placeholder.",
        ),
        ProviderStatusOut(
            name="ollama-local",
            configured=bool(s.ollama_enabled),
            description="Ollama in-cluster (medgemma, gemma4). Costo wholesale = stima OPEX/volume.",
            note=(
                None
                if s.ollama_enabled
                else "Imposta BVP_OLLAMA_ENABLED=true nel cluster (e deploya il pod Ollama) per attivare i modelli in-house."
            ),
        ),
        ProviderStatusOut(
            name="stub",
            configured=True,
            description="Provider deterministico per tier=free e per i test. Sempre disponibile.",
            note=None,
        ),
    ]

    # What the tier resolver picks right now.
    callable_provider_kinds = {p.name for p in provider_rows if p.configured}

    def _tier_row(tier: AiTier) -> TierDefaultOut:
        cfg = config_for_tier(tier)
        return TierDefaultOut(
            tier=tier.value,
            provider_kind=cfg.llm_provider_kind,
            model_id=cfg.llm_model_id,
            is_callable=cfg.llm_provider_kind in callable_provider_kinds,
        )

    tier_defaults = [
        _tier_row(AiTier.FREE),
        _tier_row(AiTier.STANDARD),
        _tier_row(AiTier.PREMIUM),
    ]

    return ProviderStatusBundle(providers=provider_rows, tier_defaults=tier_defaults)
