"""Unit tests for the provider-agnostic agent loop."""

from __future__ import annotations

import pytest

from bvphoenix.services.agent_loop import (
    AgentLoopResult,
    ToolCallTrace,
    agent_loop,
)
from bvphoenix.services.llm import StubLLM
from bvphoenix.services.llm_types import LLMTool


def _tool(name: str = "find_events") -> LLMTool:
    return LLMTool(
        name=name,
        description="dummy",
        input_schema={"type": "object", "properties": {}},
    )


@pytest.mark.asyncio
async def test_loop_no_tools_runs_one_shot() -> None:
    result = await agent_loop(
        StubLLM(),
        system="sys",
        user_message="hi",
        tools=[],
        executors={},
    )
    assert isinstance(result, AgentLoopResult)
    assert result.iterations == 1
    assert result.tool_calls == []
    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_loop_runs_one_tool_then_stops() -> None:
    """Stub: first turn → tool_use; second turn (after tool_result) → end_turn."""
    received_args: list[dict] = []

    async def executor(args: dict) -> str:
        received_args.append(args)
        return "tool result text"

    tools = [_tool("find_events")]
    result = await agent_loop(
        StubLLM(),
        system="sys",
        user_message="qual'è l'ultima PET?",
        tools=tools,
        executors={"find_events": executor},
        max_iterations=4,
    )
    assert result.stop_reason == "end_turn"
    # The stub picks the first tool with empty args; one execution recorded.
    assert len(received_args) == 1
    assert len(result.tool_calls) == 1
    trace = result.tool_calls[0]
    assert isinstance(trace, ToolCallTrace)
    assert trace.name == "find_events"
    assert trace.is_error is False
    # Two iterations: tool_use turn + final assistant turn.
    assert result.iterations == 2


@pytest.mark.asyncio
async def test_loop_executor_error_becomes_tool_result() -> None:
    async def boom(args: dict) -> str:
        raise RuntimeError("simulated")

    tools = [_tool("find_events")]
    result = await agent_loop(
        StubLLM(),
        system="sys",
        user_message="x",
        tools=tools,
        executors={"find_events": boom},
        max_iterations=4,
    )
    # Loop survives; the trace reports the error.
    assert result.stop_reason == "end_turn"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].is_error is True
    assert "simulated" in (result.tool_calls[0].error_message or "")


@pytest.mark.asyncio
async def test_loop_missing_executor_rejects_upfront() -> None:
    tools = [_tool("nope")]
    with pytest.raises(ValueError, match="missing executors"):
        await agent_loop(
            StubLLM(),
            system="sys",
            user_message="x",
            tools=tools,
            executors={},
        )


@pytest.mark.asyncio
async def test_loop_max_iterations_caps_runs() -> None:
    """A misbehaving stub that always tool_uses still terminates."""

    class AlwaysToolStub(StubLLM):
        async def complete_with_tools(self, **kwargs):  # type: ignore[override]
            from bvphoenix.services.llm import LLMUsage
            from bvphoenix.services.llm_types import AgentResult, LLMToolUseBlock

            return AgentResult(
                blocks=[LLMToolUseBlock(id="x", name="find_events", input={})],
                stop_reason="tool_use",
                model_id="stub-loop",
                usage=LLMUsage(),
            )

    async def executor(args: dict) -> str:
        return "ok"

    tools = [_tool("find_events")]
    result = await agent_loop(
        AlwaysToolStub(),
        system="sys",
        user_message="x",
        tools=tools,
        executors={"find_events": executor},
        max_iterations=3,
    )
    assert result.iterations == 3
    assert result.stop_reason == "max_tokens"
    assert len(result.tool_calls) == 3
