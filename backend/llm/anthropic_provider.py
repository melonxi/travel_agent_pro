# backend/llm/anthropic_provider.py
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic
from opentelemetry import trace as otel_trace

from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL


class AnthropicProvider:
    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncAnthropic()

    def _split_system_and_convert(
        self, messages: list[Message]
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_parts.append(msg.content or "")
            elif msg.role == Role.TOOL and msg.tool_result:
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_result.tool_call_id,
                                "content": json.dumps(
                                    {
                                        "status": msg.tool_result.status,
                                        "data": msg.tool_result.data,
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                )
            elif msg.role == Role.ASSISTANT and msg.tool_calls:
                content: list[dict] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                converted.append({"role": "assistant", "content": content})
            else:
                converted.append(
                    {
                        "role": msg.role.value,
                        "content": msg.content or "",
                    }
                )

        return "\n\n".join(system_parts), converted

    def _convert_tools(self, tool_defs: list[dict]) -> list[dict[str, Any]]:
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tool_defs
        ]

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]:
        tracer = otel_trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute(LLM_PROVIDER, "anthropic")
            span.set_attribute(LLM_MODEL, self.model)
            system, converted = self._split_system_and_convert(messages)
            kwargs: dict[str, Any] = {
                "model": self.model,
                "system": system,
                "messages": converted,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if tools:
                kwargs["tools"] = self._convert_tools(tools)

            if not stream:
                response = await self.client.messages.create(**kwargs)
                for block in response.content:
                    if block.type == "text":
                        yield LLMChunk(type=ChunkType.TEXT_DELTA, content=block.text)
                    elif block.type == "tool_use":
                        yield LLMChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call=ToolCall(
                                id=block.id, name=block.name, arguments=block.input
                            ),
                        )
                yield LLMChunk(type=ChunkType.DONE)
                return

            async with self.client.messages.stream(**kwargs) as stream_resp:
                current_tool_id: str | None = None
                current_tool_name: str | None = None
                current_tool_json: str = ""

                async for event in stream_resp:
                    if event.type == "content_block_start":
                        if hasattr(event.content_block, "type"):
                            if event.content_block.type == "tool_use":
                                current_tool_id = event.content_block.id
                                current_tool_name = event.content_block.name
                                current_tool_json = ""
                    elif event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            yield LLMChunk(
                                type=ChunkType.TEXT_DELTA, content=event.delta.text
                            )
                        elif hasattr(event.delta, "partial_json"):
                            current_tool_json += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        if current_tool_id and current_tool_name:
                            yield LLMChunk(
                                type=ChunkType.TOOL_CALL_START,
                                tool_call=ToolCall(
                                    id=current_tool_id,
                                    name=current_tool_name,
                                    arguments=json.loads(current_tool_json)
                                    if current_tool_json
                                    else {},
                                ),
                            )
                            current_tool_id = None
                            current_tool_name = None
                    elif event.type == "message_stop":
                        yield LLMChunk(type=ChunkType.DONE)

    async def count_tokens(self, messages: list[Message]) -> int:
        total = 0
        for msg in messages:
            if msg.content:
                total += len(msg.content) // 3  # rough estimate for Claude
        return total
