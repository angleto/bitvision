"""Server-Sent Events (SSE) streaming endpoint for live LLM consultation.

Streams model output token-by-token so the UI can render tokens as they
arrive, rather than waiting for the full response. Tries the official
``anthropic`` SDK (``AsyncAnthropic().messages.stream``) when available
and falls back to a direct ``httpx`` streaming call against the Messages
API. The two paths emit the same normalised SSE envelope.

Wire protocol (``text/event-stream``):

    event: token
    data: {"text": "..."}

    event: usage
    data: {"prompt_tokens": N, "completion_tokens": N, "cache_read_tokens": N}

    event: done
    data: {}

Permissions: requires an authenticated user. When ``study_id`` is present
in the body, ``run:llm`` is enforced against that study, mirroring
``POST /api/series/{id}/llm/describe``. Without ``study_id`` the endpoint
is usable for free-form consultation chat — any authenticated user can
call it; cost/abuse is gated by auth + upstream rate limits.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import ImagingStudy, User
from bvphoenix.db.session import get_db
from bvphoenix.services.permissions import RUN_LLM, can
from bvphoenix.services.rate_limit import LLM_LIMIT, limiter

router = APIRouter(tags=["llm"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class StreamIn(BaseModel):
    system: str | None = Field(default=None, max_length=20000)
    messages: list[ChatMessage] = Field(default_factory=list, min_length=1)
    lang: str | None = Field(default=None, max_length=16)
    cache_control: bool = Field(
        default=False,
        description=(
            "When true, mark the system prompt (and the first user message) "
            "as ephemeral cache breakpoints so long shared prefixes become "
            "cache hits on the next turn. See Anthropic prompt caching."
        ),
    )
    study_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional study to bind the chat to. When set, the caller must "
            "have run:llm on it — matching /api/series/{id}/llm/describe."
        ),
    )
    max_tokens: int = Field(default=1024, ge=1, le=8192)


def _sse_pack(event: str, data: dict[str, Any]) -> bytes:
    """Encode one SSE event. Always terminated with a blank line."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _sse_terminal(
    *, prompt_tokens: int, completion_tokens: int, cache_read_tokens: int
) -> tuple[bytes, bytes]:
    """The two trailing events every stream emits: usage + done."""
    return (
        _sse_pack(
            "usage",
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_read_tokens": cache_read_tokens,
            },
        ),
        _sse_pack("done", {}),
    )


def _build_anthropic_payload(body: StreamIn, model_id: str) -> dict[str, Any]:
    """Shape the Messages-API request body.

    ``cache_control`` promotes the system prompt (and the first user
    message) to ephemeral cache breakpoints — the rest of the context
    reuses the cached prefix on subsequent turns.
    """
    messages: list[dict[str, Any]] = []
    for i, m in enumerate(body.messages):
        if body.cache_control and i == 0 and m.role == "user":
            messages.append(
                {
                    "role": m.role,
                    "content": [
                        {
                            "type": "text",
                            "text": m.content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            )
        else:
            messages.append({"role": m.role, "content": m.content})

    payload: dict[str, Any] = {
        "model": model_id,
        "max_tokens": body.max_tokens,
        "messages": messages,
        "stream": True,
    }
    system_text = body.system
    if body.lang:
        prefix = f"Respond in language: {body.lang}.\n\n"
        system_text = prefix + (system_text or "")
    if system_text:
        if body.cache_control:
            payload["system"] = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            payload["system"] = system_text
    return payload


async def _stream_anthropic_sdk(
    payload: dict[str, Any], *, api_key: str
) -> AsyncIterator[bytes] | None:
    """Stream via the official SDK if installed; returns ``None`` when the
    module is not importable so the caller can fall back to httpx."""
    try:
        from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
    except ImportError:
        return None

    async def gen() -> AsyncIterator[bytes]:
        client = AsyncAnthropic(api_key=api_key)
        kwargs = {k: v for k, v in payload.items() if k != "stream"}
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield _sse_pack("token", {"text": text})
                final = await stream.get_final_message()
                usage = getattr(final, "usage", None)
                if usage is not None:
                    prompt_tokens = getattr(usage, "input_tokens", 0) or 0
                    completion_tokens = getattr(usage, "output_tokens", 0) or 0
                    cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        except Exception as exc:  # pragma: no cover — surface upstream errors to UI
            yield _sse_pack("error", {"message": str(exc)})
        usage_evt, done_evt = _sse_terminal(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        yield usage_evt
        yield done_evt

    return gen()


async def _stream_anthropic_httpx(payload: dict[str, Any], *, api_key: str) -> AsyncIterator[bytes]:
    """Direct SSE streaming via httpx — fallback when the SDK is absent.

    Parses the Anthropic event stream (``message_start``,
    ``content_block_delta``, ``message_delta``, ``message_stop``) and
    re-emits only the text deltas + a terminal usage event.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    prompt_tokens = 0
    completion_tokens = 0
    cache_read_tokens = 0

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None)) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                content=json.dumps(payload),
            ) as resp:
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="replace")
                    yield _sse_pack(
                        "error",
                        {"status": resp.status_code, "message": err_body[:2000]},
                    )
                    yield _sse_pack("done", {})
                    return

                current_event: str | None = None
                async for line in resp.aiter_lines():
                    if not line:
                        current_event = None
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:") :].strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    evt = current_event or obj.get("type")
                    if evt == "content_block_delta":
                        delta = obj.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                yield _sse_pack("token", {"text": text})
                    elif evt == "message_start":
                        usage = (obj.get("message") or {}).get("usage") or {}
                        prompt_tokens = usage.get("input_tokens", prompt_tokens) or 0
                        cache_read_tokens = (
                            usage.get("cache_read_input_tokens", cache_read_tokens) or 0
                        )
                    elif evt == "message_delta":
                        usage = obj.get("usage") or {}
                        if "output_tokens" in usage:
                            completion_tokens = usage.get("output_tokens") or 0
                    elif evt == "message_stop":
                        break
    except httpx.HTTPError as exc:
        yield _sse_pack("error", {"message": f"upstream transport error: {exc}"})

    usage_evt, done_evt = _sse_terminal(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
    )
    yield usage_evt
    yield done_evt


async def _stub_stream(body: StreamIn) -> AsyncIterator[bytes]:
    """Deterministic stream for dev / CI when no API key is configured.

    Emits the last user message back word-by-word so the UI contract can
    be exercised end-to-end without leaking secrets or hitting the network.
    """
    last_user = next((m.content for m in reversed(body.messages) if m.role == "user"), "")
    prefix = "[stub] You said: "
    for word in (prefix + last_user).split(" "):
        yield _sse_pack("token", {"text": word + " "})
    usage_evt, done_evt = _sse_terminal(
        prompt_tokens=sum(len(m.content) for m in body.messages) // 4,
        completion_tokens=len(last_user) // 4,
        cache_read_tokens=0,
    )
    yield usage_evt
    yield done_evt


@router.post("/llm/stream")
@limiter.limit(LLM_LIMIT)
async def llm_stream(
    request: Request,
    body: StreamIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> StreamingResponse:
    """Stream a chat completion as SSE.

    When ``study_id`` is supplied the caller must hold ``run:llm`` on
    that study (same gate as ``POST /api/series/{id}/llm/describe``).
    Otherwise any authenticated user can use the endpoint — the chat is
    not bound to a Health Record and spend is controlled upstream.
    """
    if body.study_id is not None:
        study = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == body.study_id))
        ).scalar_one_or_none()
        if study is None:
            raise HTTPException(status_code=404, detail="study not found")
        enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
        if not await can(db, user=user, action=RUN_LLM, study=study):
            raise HTTPException(status_code=403, detail="run:llm not permitted")

    settings = get_settings()

    # SSE-friendly headers: disable buffering in nginx and disable caching
    # so intermediate proxies actually flush each event to the client.
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

    if settings.llm_provider != "anthropic" or not settings.anthropic_api_key:
        return StreamingResponse(
            _stub_stream(body),
            media_type="text/event-stream",
            headers=headers,
        )

    payload = _build_anthropic_payload(body, settings.llm_default_model)
    sdk_stream = await _stream_anthropic_sdk(payload, api_key=settings.anthropic_api_key)
    gen: AsyncIterator[bytes]
    if sdk_stream is not None:
        gen = sdk_stream
    else:
        gen = _stream_anthropic_httpx(payload, api_key=settings.anthropic_api_key)

    return StreamingResponse(gen, media_type="text/event-stream", headers=headers)
