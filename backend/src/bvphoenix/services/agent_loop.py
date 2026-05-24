"""Provider-agnostic tool-use loop for the Q&A orchestrator.

The loop drives a multi-turn conversation between an :class:`LLMProvider`
and a fixed catalog of tools:

1. Send the conversation so far to ``complete_with_tools``.
2. If the result has ``stop_reason='end_turn'``, return.
3. Otherwise, the result contains one or more :class:`LLMToolUseBlock`
   blocks. For each, run the registered Python ``executor`` and append
   the resulting :class:`LLMToolResultBlock` to the conversation.
4. Repeat from step 1, up to ``max_iterations``.

The loop is *provider-agnostic*: it does not import any concrete
provider class. The single :class:`LLMProvider` Protocol surface plus
the neutral block dataclasses are enough. Same loop drives Anthropic,
OpenAI, Scaleway, and Ollama paths.

Tool executors are async callables ``async (input: dict) -> str``.
They MUST NOT receive the patient_id from the LLM; the orchestrator
binds it in the closure when registering each executor (defence in
depth — cross-patient must be impossible by construction). Errors
raised by an executor are caught and surfaced as a tool_result block
with ``is_error=True`` so the model can see and recover from them in
the next turn.

Token-budget enforcement: the loop sums all per-iteration usage into
a cumulative :class:`LLMUsage`. When the running prompt-token total
exceeds ``token_budget`` the loop terminates early with
``stop_reason='max_tokens'``.

A trace of every tool call (name, args sanitised, duration, result
size, error flag) is collected so the caller can audit the run via
``provenance_events`` and surface a "tool plan" in the GUI.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from bvphoenix.services.llm import LLMProvider, LLMUsage
from bvphoenix.services.llm_types import (
    AgentResult,
    LLMTextBlock,
    LLMTool,
    LLMToolUseBlock,
    ToolStopReason,
)

logger = logging.getLogger(__name__)


ToolExecutor = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolCallTrace:
    """Audit record for one executed tool call."""

    name: str
    args: dict[str, Any]
    duration_ms: int
    result_chars: int
    is_error: bool
    error_message: str | None = None


@dataclass
class AgentLoopResult:
    """Outcome of the full loop.

    ``final_text`` is the concatenation of every text block emitted by
    the assistant across all turns — typically only the last turn
    contributes once ``stop_reason='end_turn'``, but some providers
    may interleave text and tool_use within a single turn.
    """

    final_text: str
    stop_reason: ToolStopReason
    iterations: int
    usage: LLMUsage
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    model_id: str | None = None


# Maximum chars we feed back into the LLM as a tool_result. Long tool
# returns are truncated with a marker so the conversation does not
# explode the context window. The orchestrator can still log the full
# result via the trace.
TOOL_RESULT_CHAR_CAP = 4000

# Hard ceilings — defensive, not the operating point. Tier-aware budgets
# are enforced by the orchestrator that owns the call.
DEFAULT_MAX_ITERATIONS = 6
DEFAULT_TOKEN_BUDGET = 30_000


async def agent_loop(
    provider: LLMProvider,
    *,
    system: str,
    user_message: str,
    tools: Sequence[LLMTool],
    executors: dict[str, ToolExecutor],
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    cache_control: bool = False,
    max_tokens_per_turn: int = 1024,
) -> AgentLoopResult:
    """Run the tool-use loop until the model decides it is done.

    Parameters
    ----------
    provider:
        Any :class:`LLMProvider` implementation that supports
        ``complete_with_tools``.
    system:
        System prompt — the orchestrator builds this with the role
        instructions and citation rules.
    user_message:
        Initial user question.
    tools:
        Tool catalog as known to the model. Each tool's ``name`` must
        match a key in ``executors``.
    executors:
        ``name → async callable(input_dict) -> str`` map. Returning
        non-string output coerces via ``str(...)``. Raising surfaces
        as a tool_result with ``is_error=True``.
    max_iterations:
        Hard cap on assistant turns. Typical value 6.
    token_budget:
        Cumulative prompt tokens above which the loop stops.
    cache_control:
        Forwarded to the provider for prompt caching.
    max_tokens_per_turn:
        Per-call ``max_tokens`` for the model output.
    """
    if not tools:
        # No tools means a degenerate one-shot — preserve the loop
        # entry point so callers can use the same helper.
        result = await provider.complete_with_tools(
            system=system,
            messages=[{"role": "user", "content": user_message}],
            tools=tools,
            cache_control=cache_control,
            max_tokens=max_tokens_per_turn,
        )
        return _result_from_single_turn(result, iterations=1)

    missing = [t.name for t in tools if t.name not in executors]
    if missing:
        raise ValueError(f"missing executors for tools: {missing}")

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    cumulative_usage = LLMUsage()
    traces: list[ToolCallTrace] = []
    final_text_parts: list[str] = []
    stop_reason: ToolStopReason = "end_turn"
    last_model_id: str | None = None
    iteration = 0

    for iter_no in range(1, max_iterations + 1):
        iteration = iter_no
        result = await provider.complete_with_tools(
            system=system,
            messages=messages,
            tools=tools,
            cache_control=cache_control,
            max_tokens=max_tokens_per_turn,
        )
        last_model_id = result.model_id
        cumulative_usage = _accumulate(cumulative_usage, result.usage)

        # Append text blocks (if any) to the running answer.
        for block in result.blocks:
            if isinstance(block, LLMTextBlock) and block.text:
                final_text_parts.append(block.text)

        if result.stop_reason in ("end_turn", "max_tokens"):
            stop_reason = result.stop_reason
            break

        if result.stop_reason == "tool_use":
            tool_calls = [b for b in result.blocks if isinstance(b, LLMToolUseBlock)]
            if not tool_calls:
                stop_reason = "end_turn"
                break

            # Echo the assistant's tool_use turn back into the
            # conversation, then append the corresponding tool_result
            # blocks as a new user turn.
            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in result.blocks],
                }
            )
            tool_results: list[dict[str, Any]] = []
            for call in tool_calls:
                executor = executors[call.name]
                trace, output, is_error = await _run_executor(executor, call)
                traces.append(trace)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": _truncate(output),
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unknown stop reason: be conservative and stop the loop.
            stop_reason = result.stop_reason
            break

        # Token-budget guard: stop before the next provider call if we
        # have already burned past the budget.
        if cumulative_usage.prompt >= token_budget:
            stop_reason = "max_tokens"
            break
    else:
        # Loop exited because we hit max_iterations without a stop.
        stop_reason = "max_tokens"

    return AgentLoopResult(
        final_text="".join(final_text_parts).strip(),
        stop_reason=stop_reason,
        iterations=iteration,
        usage=cumulative_usage,
        tool_calls=traces,
        model_id=last_model_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_executor(
    executor: ToolExecutor, call: LLMToolUseBlock
) -> tuple[ToolCallTrace, str, bool]:
    """Run one executor with timing, errors-as-results semantics."""
    started = time.perf_counter()
    try:
        raw = await executor(call.input)
        out = str(raw)
        duration_ms = int((time.perf_counter() - started) * 1000)
        return (
            ToolCallTrace(
                name=call.name,
                args=_sanitise_args(call.input),
                duration_ms=duration_ms,
                result_chars=len(out),
                is_error=False,
            ),
            out,
            False,
        )
    except asyncio.CancelledError:
        # Caller is shutting us down (client disconnect, timeout) —
        # propagate so the loop surrounding us stops cleanly.
        raise
    except Exception as exc:
        logger.exception("tool executor %s raised", call.name)
        msg = f"{type(exc).__name__}: {exc}"
        duration_ms = int((time.perf_counter() - started) * 1000)
        return (
            ToolCallTrace(
                name=call.name,
                args=_sanitise_args(call.input),
                duration_ms=duration_ms,
                result_chars=len(msg),
                is_error=True,
                error_message=msg,
            ),
            msg,
            True,
        )


def _result_from_single_turn(result: AgentResult, *, iterations: int) -> AgentLoopResult:
    final = "".join(b.text for b in result.blocks if isinstance(b, LLMTextBlock)).strip()
    return AgentLoopResult(
        final_text=final,
        stop_reason=result.stop_reason,
        iterations=iterations,
        usage=result.usage,
        tool_calls=[],
        model_id=result.model_id,
    )


def _accumulate(a: LLMUsage, b: LLMUsage) -> LLMUsage:
    return LLMUsage(
        prompt=a.prompt + b.prompt,
        completion=a.completion + b.completion,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
    )


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Serialise a neutral block back into the message-dict shape that
    every provider adapter knows how to translate."""
    if isinstance(block, LLMTextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, LLMToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    return {"type": "text", "text": str(block)}


def _truncate(s: str, *, cap: int = TOOL_RESULT_CHAR_CAP) -> str:
    if len(s) <= cap:
        return s
    return s[: cap - 1].rstrip() + "…"


def _sanitise_args(args: dict[str, Any]) -> dict[str, Any]:
    """Strip large blobs from the trace payload.

    Arguments may legitimately contain large strings (the model
    sometimes inlines retrieved text). For audit we keep the structure
    but truncate any value past 200 chars.
    """
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "…"
        else:
            out[k] = v
    return out


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_TOKEN_BUDGET",
    "TOOL_RESULT_CHAR_CAP",
    "AgentLoopResult",
    "ToolCallTrace",
    "ToolExecutor",
    "agent_loop",
]
