# backend/agent/loop.py
from __future__ import annotations

from typing import AsyncIterator

from opentelemetry import trace

from agent.hooks import HookManager
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import AGENT_PHASE, AGENT_ITERATION
from tools.engine import ToolEngine


class AgentLoop:
    def __init__(
        self,
        llm,
        tool_engine: ToolEngine,
        hooks: HookManager,
        max_retries: int = 3,
    ):
        self.llm = llm
        self.tool_engine = tool_engine
        self.hooks = hooks
        self.max_retries = max_retries

    async def run(
        self,
        messages: list[Message],
        phase: int,
        tools_override: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("agent_loop.run") as span:
            span.set_attribute(AGENT_PHASE, phase)
            tools = tools_override or self.tool_engine.get_tools_for_phase(phase)

            for iteration in range(self.max_retries):  # safety limit on loop iterations
                with tracer.start_as_current_span("agent_loop.iteration") as iter_span:
                    iter_span.set_attribute(AGENT_ITERATION, iteration)

                    await self.hooks.run(
                        "before_llm_call", messages=messages, phase=phase
                    )

                    tool_calls: list[ToolCall] = []
                    text_chunks: list[str] = []

                    async for chunk in self.llm.chat(
                        messages, tools=tools, stream=True
                    ):
                        if chunk.type == ChunkType.TEXT_DELTA:
                            text_chunks.append(chunk.content or "")
                            yield chunk
                        elif (
                            chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call
                        ):
                            tool_calls.append(chunk.tool_call)
                            yield chunk
                        elif chunk.type == ChunkType.DONE:
                            pass

                    # If no tool calls, we're done — the LLM gave a final text response
                    if not tool_calls:
                        full_text = "".join(text_chunks)
                        if full_text:
                            messages.append(
                                Message(role=Role.ASSISTANT, content=full_text)
                            )
                        yield LLMChunk(type=ChunkType.DONE)
                        return

                    # Record assistant message with tool calls
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content="".join(text_chunks) or None,
                            tool_calls=tool_calls,
                        )
                    )

                    # Execute each tool call
                    for tc in tool_calls:
                        result = await self.tool_engine.execute(tc)

                        messages.append(
                            Message(
                                role=Role.TOOL,
                                tool_result=result,
                            )
                        )

                        # Keepalive ping so the SSE connection stays alive during
                        # back-to-back tool executions that produce no text output
                        yield LLMChunk(type=ChunkType.KEEPALIVE)

                        await self.hooks.run(
                            "after_tool_call",
                            tool_name=tc.name,
                            tool_call=tc,
                            result=result,
                        )

                    # Loop continues — LLM will see tool results and decide next step

            # Safety limit reached
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA, content="[达到最大循环次数，请重新发送消息]"
            )
            yield LLMChunk(type=ChunkType.DONE)
