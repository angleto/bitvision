"""Provider-neutral types for the agentic LLM API.

Two surfaces coexist on every concrete :class:`LLMProvider`:

* The legacy *single-shot* ``complete()`` / ``summarize()`` calls that
  return plain text. Defined alongside :class:`LLMUsage` and
  :class:`LLMResult` in :mod:`bvphoenix.services.llm`.
* The new *agentic* ``complete_with_tools()`` / ``stream_with_tools()``
  calls used by the Q&A orchestrator. They exchange typed content
  blocks (``text`` and ``tool_use``) and tool-result messages so the
  loop can drive multi-turn function calling.

The types in this module describe that agentic surface. They are
*neutral* — they do not encode Anthropic's, OpenAI's, or Scaleway's
serialisation choices. Each provider adapter translates to/from them.
A consumer of an :class:`AgentResult` should never need to know which
backend produced it.

Streaming is exposed via :data:`LLMStreamEvent`, a discriminated union
of incremental events that the SSE wrapper in
``backend/src/bvphoenix/api/qna.py`` mirrors verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from bvphoenix.services.llm import LLMUsage

__all__ = [
    "AgentResult",
    "LLMResponseBlock",
    "LLMStreamCitation",
    "LLMStreamDelta",
    "LLMStreamDone",
    "LLMStreamError",
    "LLMStreamEvent",
    "LLMStreamToolCallEnd",
    "LLMStreamToolCallStart",
    "LLMTextBlock",
    "LLMTool",
    "LLMToolResultBlock",
    "LLMToolUseBlock",
    "ToolStopReason",
]


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMTool:
    """A tool the model is allowed to call.

    ``input_schema`` is a JSON Schema 2020-12 object that constrains the
    arguments. Each provider adapter rewrites it into its native
    function-calling shape (Anthropic ``tools=[{name, input_schema}]``,
    OpenAI ``tools=[{type:'function', function:{parameters}}]``).
    """

    name: str
    description: str
    input_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# Content blocks (assistant + user messages)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMTextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class LLMToolUseBlock:
    """Assistant request to call a tool.

    ``id`` is the provider-emitted call identifier; the loop must echo
    it back verbatim on the matching :class:`LLMToolResultBlock` so the
    provider can correlate request and response across turns.
    """

    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class LLMToolResultBlock:
    """Caller-supplied result for a previous tool_use block."""

    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


LLMResponseBlock = LLMTextBlock | LLMToolUseBlock


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


ToolStopReason = Literal["end_turn", "tool_use", "max_tokens", "error", "cancelled"]


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Single-turn assistant response in the agentic API.

    Contains the raw block list (text + tool_use), the stop reason, and
    the token-usage shard. The agent loop reads ``stop_reason`` to
    decide whether to feed tool results back and re-call the provider.
    """

    blocks: list[LLMResponseBlock]
    stop_reason: ToolStopReason
    model_id: str
    usage: LLMUsage = field(default_factory=LLMUsage)


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMStreamToolCallStart:
    call_id: str
    tool_name: str
    args: dict[str, Any]
    type: Literal["tool_call_start"] = "tool_call_start"


@dataclass(frozen=True, slots=True)
class LLMStreamToolCallEnd:
    call_id: str
    duration_ms: int
    result_summary: str
    is_error: bool = False
    type: Literal["tool_call_end"] = "tool_call_end"


@dataclass(frozen=True, slots=True)
class LLMStreamDelta:
    delta: str
    type: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True, slots=True)
class LLMStreamCitation:
    """Citation reference emitted alongside the assistant text.

    The orchestrator parses the model's inline ``[doc:UUID]`` /
    ``[event:UUID]`` markers and emits one of these per citation so the
    frontend can render clickable provenance chips without re-parsing.
    """

    kind: Literal["document", "event", "report", "note", "summary", "chunk"]
    ref_id: str
    label: str | None = None
    type: Literal["citation"] = "citation"


@dataclass(frozen=True, slots=True)
class LLMStreamDone:
    stop_reason: ToolStopReason
    usage: LLMUsage = field(default_factory=LLMUsage)
    model_id: str | None = None
    type: Literal["done"] = "done"


@dataclass(frozen=True, slots=True)
class LLMStreamError:
    code: str
    message: str
    type: Literal["error"] = "error"


LLMStreamEvent = (
    LLMStreamToolCallStart
    | LLMStreamToolCallEnd
    | LLMStreamDelta
    | LLMStreamCitation
    | LLMStreamDone
    | LLMStreamError
)
