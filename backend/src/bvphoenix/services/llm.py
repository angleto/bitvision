"""LLM provider abstraction.

Strategy pattern (DESIGN.md §7) — concrete providers implement
``LLMProvider`` (a ``typing.Protocol``) and are selected via the
``BVP_LLM_PROVIDER`` env var. ``"stub"`` returns a deterministic mock so
the rest of the system can be exercised without real API keys.

The low-level ``complete()`` primitive exposes:

* ``system`` — a stable system prompt (good cache target).
* ``messages`` — the conversation so far. The caller is expected to put
  stable bulk content (fascicolo bundle, retrieved context) into an
  early user message and flag it with ``"cache": True``; the live user
  question goes in a separate, unflagged message so it does not force a
  cache miss on the prefix.
* ``cache_control`` — master switch. When ``True`` the system prompt
  and any user message marked ``"cache": True`` get a
  ``cache_control={"type": "ephemeral"}`` breakpoint.
* ``stream`` — reserved. Not used by any caller in this slice.

``describe_series()`` is kept as a thin adapter so ``api/llm.py`` and
the existing unit tests do not need to change. New callers
(U1/U2 summaries) go through ``complete()`` and consume
``LLMResult.usage`` to record token costs on the ``Consultation`` row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from bvphoenix.config import get_settings

if TYPE_CHECKING:
    from bvphoenix.services.llm_types import (
        AgentResult,
        LLMResponseBlock,
        LLMStreamEvent,
        LLMTool,
        LLMToolResultBlock,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token usage for a single Messages API call.

    ``prompt`` / ``completion`` are the raw input/output token counts.
    ``cache_read_tokens`` is what the provider served from cache —
    billed at ~10% of input cost. ``cache_creation_tokens`` is the
    first-time write into the cache (~125% of input cost) and only
    shows up on the call that *populates* the cache.
    """

    prompt: int = 0
    completion: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Unified return shape for ``LLMProvider.complete()``."""

    text: str
    model_id: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DescriptionResult:
    """Kept for the legacy ``describe_series()`` call path (api/llm.py)."""

    text: str
    model_id: str
    confidence: float | None = None
    token_usage: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """Output of a free-form ``summarize`` call.

    ``token_usage`` is a best-effort dict (``{"input": int, "output":
    int}`` for Anthropic, empty for the stub) so callers can record
    cost without branching on the provider.
    """

    text: str
    model_id: str
    token_usage: dict[str, int] | None = None


def _lang_instruction(lang: str) -> str:
    """Normalized system-prompt suffix controlling the reply language.

    Accepts the common ISO-639 codes used across the UI ("it", "en") and
    falls back to a neutral English directive for anything else so the
    downstream LLM never receives an empty instruction.
    """
    code = (lang or "").strip().lower()
    if code in {"it", "it-it", "ita", "italian", "italiano"}:
        return "Rispondi sempre in italiano."
    if code in {"en", "en-us", "en-gb", "eng", "english"}:
        return "Always reply in English."
    return f"Always reply in the language identified by the code '{code or 'en'}'."


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal provider surface.

    ``complete()`` is the strategic primitive; ``describe_series()`` is
    kept as a thin legacy adapter so the descriptor endpoint and its
    tests do not need to change in this slice.
    """

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cache_control: bool = False,
        stream: bool = False,
        max_tokens: int = 1024,
    ) -> LLMResult: ...

    async def describe_series(
        self,
        *,
        modality: str | None,
        body_part: str | None,
        hint: str | None,
        lang: str = "it",
    ) -> DescriptionResult: ...

    async def summarize_fascicolo(
        self,
        *,
        patient_label: str,
        bundle: dict,
        lang: str = "it",
    ) -> DescriptionResult: ...

    async def summarize(
        self, *, system: str, user_prompt: str, max_tokens: int = 1024
    ) -> SummaryResult: ...

    # Agentic API. Implementations may translate to provider-native tool
    # calling (Anthropic ``tools=``, OpenAI ``function calling``) or, for
    # providers without native support (e.g. Ollama), simulate it with a
    # JSON-only prompt and parse the reply.
    async def complete_with_tools(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[LLMTool],
        tool_choice: str = "auto",
        cache_control: bool = False,
        max_tokens: int = 1024,
    ) -> AgentResult: ...

    def stream_with_tools(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[LLMTool],
        tool_choice: str = "auto",
        cache_control: bool = False,
        max_tokens: int = 1024,
    ) -> AsyncIterator[LLMStreamEvent]: ...


# ---------------------------------------------------------------------------
# Stub — zero-cost provider for dev/test
# ---------------------------------------------------------------------------


class StubLLM:
    """Deterministic no-network provider.

    ``complete()`` echoes the last user message back so callers can
    assert on structure without a real key. ``describe_series()`` keeps
    its original synthetic-sentence shape so existing unit tests pass.
    """

    model_id: str = "stub-v0"

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cache_control: bool = False,
        stream: bool = False,
        max_tokens: int = 1024,
    ) -> LLMResult:
        last = messages[-1] if messages else {}
        content = last.get("content", "")
        if isinstance(content, list):
            # Extract text blocks for the stub echo.
            content = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        text = f"[stub] system={system[:40]!r} last_user={str(content)[:80]!r}"
        return LLMResult(
            text=text,
            model_id=self.model_id,
            usage=LLMUsage(),
            stop_reason="end_turn",
        )

    async def describe_series(
        self,
        *,
        modality: str | None,
        body_part: str | None,
        hint: str | None,
        lang: str = "it",
    ) -> DescriptionResult:
        bits = [
            modality or "imaging study",
            f"of the {body_part.lower()}" if body_part else "of an unspecified region",
        ]
        if hint:
            bits.append(f"with note: {hint}")
        return DescriptionResult(
            text=f"Synthetic description [{lang}] — " + " ".join(bits) + ".",
            model_id=self.model_id,
            confidence=0.0,
        )

    async def summarize_fascicolo(
        self,
        *,
        patient_label: str,
        bundle: dict,
        lang: str = "it",
    ) -> DescriptionResult:
        sections = bundle.get("index", {}).get("sections") or []
        timeline = bundle.get("timeline") or []
        docs = bundle.get("documents") or []
        is_it = lang.strip().lower().startswith("it")
        labels = {
            "patient": "Paziente" if is_it else "Patient",
            "last": "ultima data" if is_it else "last date",
            "timeline": "Timeline",
            "events": "eventi registrati" if is_it else "events on record",
            "docs": "Documenti clinici allegati" if is_it else "Attached clinical documents",
            "na": "n/a",
        }
        bullets = [f"- {labels['patient']}: {patient_label}."]
        for s in sections[:3]:
            bullets.append(
                f"- {s.get('label', s.get('key'))}: {s.get('count', 0)} "
                f"({labels['last']}: {s.get('last_date') or labels['na']})."
            )
        bullets.append(f"- {labels['timeline']}: {len(timeline)} {labels['events']}.")
        bullets.append(f"- {labels['docs']}: {len(docs)}.")
        return DescriptionResult(
            text="\n".join(bullets),
            model_id="stub-v0",
            confidence=0.0,
            token_usage=LLMUsage().as_dict(),
        )

    async def summarize(
        self, *, system: str, user_prompt: str, max_tokens: int = 1024
    ) -> SummaryResult:
        # Deterministic stub so tests & dev flows don't depend on an API
        # key. The first 400 chars of the input are enough to verify the
        # caller assembled the prompt correctly.
        snippet = user_prompt.strip().replace("\n", " ")[:400]
        return SummaryResult(
            text=f"[stub-summary] {snippet}",
            model_id="stub-v0",
            token_usage={"input": 0, "output": 0},
        )

    async def complete_with_tools(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[LLMTool],
        tool_choice: str = "auto",
        cache_control: bool = False,
        max_tokens: int = 1024,
    ) -> AgentResult:
        """Deterministic stub for the agentic API.

        Strategy: if the conversation already has a tool_result block,
        emit a final text message and stop; otherwise, call the *first*
        tool with empty arguments. This reliably exercises the loop
        wiring (one tool call + one result + one final assistant text)
        without any provider dependencies.
        """
        from bvphoenix.services.llm_types import (
            AgentResult,
            LLMTextBlock,
            LLMToolUseBlock,
        )

        already_called = any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for m in messages
            for block in (m.get("content") or [])
            if isinstance(m, dict) and isinstance(m.get("content"), list)
        )

        if already_called or not tools:
            return AgentResult(
                blocks=[LLMTextBlock(text=f"[stub-agent] system={system[:40]!r}")],
                stop_reason="end_turn",
                model_id=self.model_id,
                usage=LLMUsage(),
            )

        first = tools[0]
        return AgentResult(
            blocks=[LLMToolUseBlock(id="stub-call-1", name=first.name, input={})],
            stop_reason="tool_use",
            model_id=self.model_id,
            usage=LLMUsage(),
        )

    async def stream_with_tools(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[LLMTool],
        tool_choice: str = "auto",
        cache_control: bool = False,
        max_tokens: int = 1024,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Stub stream: emit a single text delta + done event."""
        from bvphoenix.services.llm_types import LLMStreamDelta, LLMStreamDone

        yield LLMStreamDelta(delta=f"[stub-stream] system={system[:30]!r}")
        yield LLMStreamDone(stop_reason="end_turn", model_id=self.model_id)


# Back-compat aliases for the pre-migration class names.
StubProvider = StubLLM


# ---------------------------------------------------------------------------
# Anthropic — SDK-backed provider with prompt caching
# ---------------------------------------------------------------------------


def _as_cacheable_blocks(content: Any, *, cache: bool) -> list[dict[str, Any]]:
    """Normalise a user/assistant ``content`` into Anthropic block form.

    Accepts a bare string (becomes one ``text`` block) or a list whose
    items are strings (coerced to ``text`` blocks) or dicts (passed
    through after stripping the internal ``"cache"`` marker).

    Anthropic places the cache breakpoint at the tagged block; everything
    preceding the marker is eligible for reuse. Individual blocks opt in
    via ``"cache": True`` on their source dict; when ``cache`` is true
    and no per-block flag was supplied, the final block is tagged as a
    sensible default. Callers should structure their messages so the
    stable bundle comes first and the live question is sent as a
    separate, untagged message.
    """
    blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                blocks.append({"type": "text", "text": b})
            elif isinstance(b, dict):
                clean = {k: v for k, v in b.items() if k != "cache"}
                if b.get("cache"):
                    clean["cache_control"] = {"type": "ephemeral"}
                blocks.append(clean)
            else:
                blocks.append({"type": "text", "text": str(b)})
    else:
        blocks.append({"type": "text", "text": str(content)})

    if cache and blocks and not any("cache_control" in b for b in blocks):
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


class AnthropicLLM:
    """Anthropic Messages API via the official async SDK.

    Prompt caching ( ``cache_control=ephemeral`` ) is applied to the
    system prompt and — by convention — to any *user* message whose
    content has been explicitly marked as cacheable (last block wins).
    The live user question must be sent as a *separate* user message
    with no cache marker so it does not force a cache miss on the
    stable prefix.
    """

    def __init__(self, *, api_key: str, model_id: str) -> None:
        # Import lazily so the stub path works in environments that
        # haven't installed the SDK (CI without extras, for instance).
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self.model_id = model_id

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cache_control: bool = False,
        stream: bool = False,
        max_tokens: int = 1024,
    ) -> LLMResult:
        # Build the system parameter. When caching, pass as a block list
        # so we can attach ``cache_control`` to it; otherwise a bare
        # string is simpler and equivalent on the API side.
        if cache_control and system:
            system_param: Any = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system

        # Rebuild messages as block lists. We tag only *user* messages
        # that the caller explicitly flagged cacheable — either via a
        # top-level ``"cache": True`` on the message, or via a
        # ``"cache": True`` on one of its content blocks. Assistant
        # turns are left alone; they are generally not large enough to
        # justify a breakpoint.
        out_messages: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            msg_cache = bool(m.get("cache")) and cache_control and role == "user"
            blocks = _as_cacheable_blocks(content, cache=msg_cache)
            out_messages.append({"role": role, "content": blocks})

        try:
            resp = await self._client.messages.create(
                model=self.model_id,
                max_tokens=max_tokens,
                system=system_param,
                messages=out_messages,
            )
        except Exception:
            logger.exception("anthropic messages.create failed")
            raise

        # Collapse text blocks into a single string — consistent with
        # the legacy httpx implementation.
        text = "".join(
            getattr(block, "text", "")
            for block in getattr(resp, "content", []) or []
            if getattr(block, "type", None) == "text"
        ).strip()

        usage_obj = getattr(resp, "usage", None)
        usage = LLMUsage(
            prompt=int(getattr(usage_obj, "input_tokens", 0) or 0),
            completion=int(getattr(usage_obj, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(usage_obj, "cache_creation_input_tokens", 0) or 0),
        )

        return LLMResult(
            text=text,
            model_id=self.model_id,
            usage=usage,
            stop_reason=getattr(resp, "stop_reason", None),
        )

    async def describe_series(
        self,
        *,
        modality: str | None,
        body_part: str | None,
        hint: str | None,
        lang: str = "it",
    ) -> DescriptionResult:
        """Legacy adapter — uses ``complete()`` with cache_control on
        the stable system prompt. The per-series metadata is sent as an
        uncached user message because each series is different enough
        that caching the user side has no payoff.
        """
        system = (
            "You are a clinical imaging assistant. Given the metadata "
            "below, draft a one-paragraph descriptor for indexing. Be "
            "conservative — do not invent findings. Reply with the "
            "descriptor only. " + _lang_instruction(lang)
        )
        user_prompt = (
            f"Modality: {modality or 'unknown'}\n"
            f"Body part: {body_part or 'unknown'}\n"
            f"Hint from uploader: {hint or 'none'}"
        )
        result = await self.complete(
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            cache_control=True,
            max_tokens=400,
        )
        return DescriptionResult(
            text=result.text,
            model_id=result.model_id,
            token_usage=result.usage.as_dict(),
        )

    async def summarize(
        self, *, system: str, user_prompt: str, max_tokens: int = 1024
    ) -> SummaryResult:
        """Free-form summarization. ``system`` primes role / style,
        ``user_prompt`` carries the concatenated source material.
        """
        result = await self.complete(
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=max_tokens,
        )
        return SummaryResult(
            text=result.text,
            model_id=result.model_id,
            token_usage=result.usage.as_dict(),
        )

    async def complete_with_tools(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[LLMTool],
        tool_choice: str = "auto",
        cache_control: bool = False,
        max_tokens: int = 1024,
    ) -> AgentResult:
        """Anthropic Messages API with native ``tools=`` function calling.

        Translates the neutral :class:`LLMTool` catalog into Anthropic's
        ``[{name, description, input_schema}]`` shape and parses the
        response back into provider-neutral
        :class:`LLMResponseBlock` objects.

        ``messages`` follow the same shape used by ``complete()`` plus
        the agentic extensions: assistant turns may carry
        ``tool_use`` blocks (passed through verbatim), and user turns
        may carry ``tool_result`` blocks with ``tool_use_id``,
        ``content`` and optional ``is_error``.
        """
        from bvphoenix.services.llm_types import (
            AgentResult,
            LLMTextBlock,
            LLMToolUseBlock,
        )

        api_tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        if cache_control and system:
            system_param: Any = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": max_tokens,
            "system": system_param,
            "messages": list(messages),
        }
        if api_tools:
            kwargs["tools"] = api_tools
            if tool_choice != "auto":
                kwargs["tool_choice"] = {"type": tool_choice}

        try:
            resp = await self._client.messages.create(**kwargs)
        except Exception:
            logger.exception("anthropic messages.create (tools) failed")
            raise

        blocks: list[Any] = []
        for block in getattr(resp, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                blocks.append(LLMTextBlock(text=getattr(block, "text", "") or ""))
            elif block_type == "tool_use":
                blocks.append(
                    LLMToolUseBlock(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=dict(getattr(block, "input", {}) or {}),
                    )
                )

        usage_obj = getattr(resp, "usage", None)
        usage = LLMUsage(
            prompt=int(getattr(usage_obj, "input_tokens", 0) or 0),
            completion=int(getattr(usage_obj, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(usage_obj, "cache_creation_input_tokens", 0) or 0),
        )
        stop = getattr(resp, "stop_reason", None) or "end_turn"
        return AgentResult(
            blocks=blocks,
            stop_reason=stop,
            model_id=self.model_id,
            usage=usage,
        )

    async def stream_with_tools(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[LLMTool],
        tool_choice: str = "auto",
        cache_control: bool = False,
        max_tokens: int = 1024,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Stream the assistant's text deltas; tool calls are surfaced
        as ``LLMStreamToolCallStart`` events. The agent loop in
        :mod:`bvphoenix.services.agent_loop` reassembles complete
        ``tool_use`` blocks from the stream when needed.
        """
        from bvphoenix.services.llm_types import (
            LLMStreamDelta,
            LLMStreamDone,
            LLMStreamToolCallStart,
        )

        api_tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        if cache_control and system:
            system_param: Any = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": max_tokens,
            "system": system_param,
            "messages": list(messages),
        }
        if api_tools:
            kwargs["tools"] = api_tools
            if tool_choice != "auto":
                kwargs["tool_choice"] = {"type": tool_choice}

        usage = LLMUsage()
        stop_reason: str = "end_turn"
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", None) == "tool_use":
                            yield LLMStreamToolCallStart(
                                call_id=getattr(block, "id", ""),
                                tool_name=getattr(block, "name", ""),
                                args={},
                            )
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", None) == "text_delta":
                            yield LLMStreamDelta(delta=getattr(delta, "text", "") or "")
                final = await stream.get_final_message()
                stop_reason = getattr(final, "stop_reason", None) or "end_turn"
                usage_obj = getattr(final, "usage", None)
                usage = LLMUsage(
                    prompt=int(getattr(usage_obj, "input_tokens", 0) or 0),
                    completion=int(getattr(usage_obj, "output_tokens", 0) or 0),
                    cache_read_tokens=int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0),
                    cache_creation_tokens=int(
                        getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
                    ),
                )
        except Exception:
            logger.exception("anthropic stream_with_tools failed")
            raise

        yield LLMStreamDone(stop_reason=stop_reason, usage=usage, model_id=self.model_id)

    async def summarize_fascicolo(
        self,
        *,
        patient_label: str,
        bundle: dict,
        lang: str = "it",
    ) -> DescriptionResult:
        system = (
            "You are a clinical assistant tasked with executive summaries "
            "of a patient's radiology record (fascicolo). Generate a concise "
            "3-5 bullet executive summary. Highlight: main findings, trends "
            "across time, and gaps/missing information. Be conservative, "
            "do not invent findings. Reply with the bullets only, one per "
            "line, starting with '- '. " + _lang_instruction(lang)
        )
        bundle_json = json.dumps(bundle, default=str, ensure_ascii=False)
        # The bulky bundle block is marked cacheable so repeated calls on
        # the same patient share the cached prefix; the short instruction
        # is sent uncached so different lang/follow-ups still reuse it.
        user_content = [
            {
                "type": "text",
                "text": f"Patient: {patient_label}\nFascicolo bundle:\n{bundle_json}",
                "cache": True,
            },
            {
                "type": "text",
                "text": (
                    "Generate a 3-5 bullet executive summary of this "
                    "patient's imaging history. Highlight main findings, "
                    "trends, and gaps."
                ),
            },
        ]
        result = await self.complete(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            cache_control=True,
            max_tokens=600,
        )
        return DescriptionResult(
            text=result.text,
            model_id=result.model_id,
            token_usage=result.usage.as_dict(),
        )


AnthropicProvider = AnthropicLLM


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    """Resolve the active provider from settings.

    ``auto`` picks the first provider whose credentials are present,
    in this preference order: scaleway, anthropic, openai, ollama. If
    nothing has a key (CI, fresh install) we fall back to StubLLM so
    the platform still boots and LLM-dependent endpoints can surface a
    graceful "feature unavailable" via their service-layer guards.

    Explicit ``BVP_LLM_PROVIDER=<name>`` skips auto-detection and
    requires the corresponding credentials. ``stub`` short-circuits
    every other path and is the recommended setting for CI.
    """
    s = get_settings()
    name = (s.llm_provider or "auto").lower()

    if name == "stub":
        return StubLLM()

    if name in ("scaleway", "auto") and s.scaleway_api_key:
        from bvphoenix.services.llm_scaleway import ScalewayLLM

        return ScalewayLLM(
            api_key=s.scaleway_api_key,
            model_id=s.scaleway_default_model,
            base_url=s.scaleway_base_url,
        )
    if name in ("anthropic", "auto") and s.anthropic_api_key:
        return AnthropicLLM(api_key=s.anthropic_api_key, model_id=s.llm_default_model)
    if name in ("openai", "auto") and s.openai_api_key:
        from bvphoenix.services.llm_openai import OpenAILLM

        return OpenAILLM(api_key=s.openai_api_key, model_id=s.openai_default_model)
    if name in ("ollama", "auto"):
        # Ollama has no credential check (local placeholder key); only
        # used when explicitly selected, never via auto-detection.
        if name == "ollama":
            from bvphoenix.services.llm_ollama import OllamaLLM

            return OllamaLLM(
                model_id=s.ollama_default_model,
                base_url=s.ollama_base_url,
            )

    return StubLLM()
