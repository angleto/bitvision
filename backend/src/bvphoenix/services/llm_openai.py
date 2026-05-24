"""OpenAI-compatible LLM provider.

A single :class:`OpenAILLM` adapter covers three deployment scenarios:

* **OpenAI proper**: ``base_url=None`` (defaults to OpenAI's own
  endpoint) + an OpenAI API key. Used when the operator picks
  ``BVP_LLM_PROVIDER=openai``.
* **Scaleway Generative API**: ``base_url=https://api.scaleway.ai/v1``
  + a Scaleway API key. Same SDK, different endpoint and model
  catalog. Wrapped by :class:`bvphoenix.services.llm_scaleway.ScalewayLLM`
  for nicer ergonomics.
* **Ollama / self-hosted**: ``base_url=http://ollama:11434/v1`` (or
  any OpenAI-compatible local server) + a placeholder API key. Wrapped
  by :class:`bvphoenix.services.llm_ollama.OllamaLLM`. Note that local
  models like medgemma do not natively support tool calling — for
  those the wrapper simulates it with a JSON prompt.

The agentic surface (``complete_with_tools`` / ``stream_with_tools``)
translates the neutral :class:`LLMTool` catalog into OpenAI's function
calling shape and parses the response back into provider-neutral
``LLMResponseBlock`` instances.

Tool-result messages are encoded in the OpenAI shape as
``{role: "tool", tool_call_id: "...", content: "..."}`` — the loop
helper in :mod:`bvphoenix.services.agent_loop` maps from the neutral
``tool_result`` block representation to this on the way out.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from bvphoenix.services.llm import (
    DescriptionResult,
    LLMResult,
    LLMUsage,
    SummaryResult,
    _lang_instruction,
)
from bvphoenix.services.llm_types import (
    AgentResult,
    LLMStreamDelta,
    LLMStreamDone,
    LLMStreamEvent,
    LLMStreamToolCallStart,
    LLMTextBlock,
    LLMTool,
    LLMToolUseBlock,
)

logger = logging.getLogger(__name__)


class OpenAILLM:
    """OpenAI Chat Completions provider with native function calling.

    Construction:
        api_key: bearer credential.
        model_id: chat-completion model id (e.g. ``gpt-4o-mini``,
            ``mistral-small-3.2-24b-instruct-2506``,
            ``medgemma:27b``).
        base_url: optional override for OpenAI-compatible endpoints.
            Pass ``None`` to use OpenAI's own.

    The class itself is concrete but stateless beyond the SDK client;
    sub-classes can override defaults (Scaleway, Ollama) without
    re-implementing the call sites.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        base_url: str | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self.model_id = model_id

    # ---- Legacy single-shot surface ----

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cache_control: bool = False,
        stream: bool = False,
        max_tokens: int = 1024,
    ) -> LLMResult:
        chat_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            chat_messages.append(_to_openai_message(m))
        try:
            resp = await self._client.chat.completions.create(
                model=self.model_id,
                messages=chat_messages,
                max_tokens=max_tokens,
            )
        except Exception:
            logger.exception("openai chat.completions.create failed")
            raise
        choice = resp.choices[0] if resp.choices else None
        text = (getattr(choice.message, "content", "") or "") if choice else ""
        usage = _usage_from_openai(getattr(resp, "usage", None))
        stop = getattr(choice, "finish_reason", None) if choice else None
        return LLMResult(
            text=text.strip(),
            model_id=self.model_id,
            usage=usage,
            stop_reason=stop,
        )

    async def describe_series(
        self,
        *,
        modality: str | None,
        body_part: str | None,
        hint: str | None,
        lang: str = "it",
    ) -> DescriptionResult:
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
        user_content = (
            f"Patient: {patient_label}\nFascicolo bundle:\n{bundle_json}\n\n"
            "Generate a 3-5 bullet executive summary of this patient's "
            "imaging history."
        )
        result = await self.complete(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=600,
        )
        return DescriptionResult(
            text=result.text,
            model_id=result.model_id,
            token_usage=result.usage.as_dict(),
        )

    # ---- Agentic surface ----

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
        chat_messages = _to_openai_message_list(system, messages)
        api_tools = [_tool_to_openai(t) for t in tools]
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "messages": chat_messages,
            "max_tokens": max_tokens,
        }
        if api_tools:
            kwargs["tools"] = api_tools
            kwargs["tool_choice"] = tool_choice if tool_choice != "auto" else "auto"
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception:
            logger.exception("openai chat.completions.create (tools) failed")
            raise

        choice = resp.choices[0] if resp.choices else None
        msg = getattr(choice, "message", None)
        blocks: list[Any] = []
        if msg is not None:
            text_content = getattr(msg, "content", None) or ""
            if text_content:
                blocks.append(LLMTextBlock(text=text_content))
            for tc in getattr(msg, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                args_raw = getattr(fn, "arguments", "{}") if fn else "{}"
                try:
                    args = json.loads(args_raw or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
                blocks.append(
                    LLMToolUseBlock(
                        id=getattr(tc, "id", "") or "",
                        name=getattr(fn, "name", "") if fn else "",
                        input=args,
                    )
                )

        finish = getattr(choice, "finish_reason", None) if choice else None
        stop_reason = _map_finish_reason(
            finish, has_tool_use=any(isinstance(b, LLMToolUseBlock) for b in blocks)
        )
        return AgentResult(
            blocks=blocks,
            stop_reason=stop_reason,
            model_id=self.model_id,
            usage=_usage_from_openai(getattr(resp, "usage", None)),
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
        """Stream text deltas and surface tool-call announcements.

        OpenAI's streaming API emits incremental ``tool_calls[].function.arguments``
        chunks; for the orchestrator's purposes it is enough to emit a
        single :class:`LLMStreamToolCallStart` when the tool name first
        appears, then let the agent loop reassemble the full call from
        the non-streamed final response. The terminal
        :class:`LLMStreamDone` carries the consolidated stop reason and
        token usage.
        """
        chat_messages = _to_openai_message_list(system, messages)
        api_tools = [_tool_to_openai(t) for t in tools]
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "messages": chat_messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if api_tools:
            kwargs["tools"] = api_tools
            kwargs["tool_choice"] = tool_choice if tool_choice != "auto" else "auto"

        usage = LLMUsage()
        stop_reason: str = "end_turn"
        announced: set[int] = set()

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    if getattr(chunk, "usage", None) is not None:
                        usage = _usage_from_openai(chunk.usage)
                    continue
                ch = chunk.choices[0]
                delta = getattr(ch, "delta", None)
                if delta is not None:
                    text_delta = getattr(delta, "content", None)
                    if text_delta:
                        yield LLMStreamDelta(delta=text_delta)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        idx = getattr(tc, "index", 0)
                        fn = getattr(tc, "function", None)
                        name = getattr(fn, "name", None) if fn else None
                        if name and idx not in announced:
                            yield LLMStreamToolCallStart(
                                call_id=getattr(tc, "id", "") or f"call-{idx}",
                                tool_name=name,
                                args={},
                            )
                            announced.add(idx)
                if getattr(ch, "finish_reason", None):
                    stop_reason = _map_finish_reason(ch.finish_reason, has_tool_use=bool(announced))
        except Exception:
            logger.exception("openai stream_with_tools failed")
            raise

        yield LLMStreamDone(stop_reason=stop_reason, usage=usage, model_id=self.model_id)


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _tool_to_openai(t: LLMTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        },
    }


def _to_openai_message(m: dict[str, Any]) -> dict[str, Any]:
    """Translate one neutral message dict into the OpenAI shape.

    The neutral shape is ``{role, content}`` where content is either a
    string or a list of content blocks. Assistant turns may carry
    ``tool_use`` blocks; user turns may carry ``tool_result`` blocks.
    OpenAI represents the latter as a separate ``role='tool'`` message
    with ``tool_call_id`` and a flat string content; the caller must
    therefore split a single neutral user-with-tool_result message into
    multiple OpenAI messages — see :func:`_to_openai_message_list`.
    """
    role = m.get("role", "user")
    content = m.get("content", "")
    if isinstance(content, str):
        return {"role": role, "content": content}

    if role == "assistant" and isinstance(content, list):
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text_parts.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {})),
                        },
                    }
                )
        out: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            out["tool_calls"] = tool_calls
        return out

    # Plain user with text-only block list: collapse to a string.
    if isinstance(content, list):
        flat = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        return {"role": role, "content": flat}

    return {"role": role, "content": str(content)}


def _to_openai_message_list(
    system: str, messages: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the full OpenAI messages array, splitting tool_result blocks
    out into ``role='tool'`` messages."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user" and isinstance(content, list):
            text_parts: list[str] = []
            tool_results: list[dict[str, Any]] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": b.get("tool_use_id", ""),
                            "content": str(b.get("content", "")),
                        }
                    )
            # Emit any accompanying user text first, then the tool replies.
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
            out.extend(tool_results)
            continue
        out.append(_to_openai_message(m))
    return out


def _usage_from_openai(usage: Any) -> LLMUsage:
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        prompt=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion=int(getattr(usage, "completion_tokens", 0) or 0),
        # OpenAI exposes prompt_tokens_details.cached_tokens for cache
        # hits when available; absent on most models, treat as 0.
        cache_read_tokens=int(
            getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        ),
        cache_creation_tokens=0,
    )


def _map_finish_reason(reason: str | None, *, has_tool_use: bool) -> str:
    if has_tool_use or reason == "tool_calls":
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "stop":
        return "end_turn"
    if reason is None:
        return "end_turn"
    return reason


__all__ = ["OpenAILLM"]
