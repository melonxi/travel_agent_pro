# backend/llm/openai_provider.py
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import tiktoken
from openai import AsyncOpenAI
from opentelemetry import trace as otel_trace

from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL


class OpenAIProvider:
    def __init__(
        self, model: str = "gpt-4o", temperature: float = 0.7, max_tokens: int = 4096
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncOpenAI()

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == Role.TOOL and msg.tool_result:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_result.tool_call_id,
                        "content": json.dumps(
                            {
                                "status": msg.tool_result.status,
                                "data": msg.tool_result.data,
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            elif msg.role == Role.ASSISTANT and msg.tool_calls:
                result.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(
                                        tc.arguments, ensure_ascii=False
                                    ),
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
            else:
                result.append(
                    {
                        "role": msg.role.value,
                        "content": msg.content or "",
                    }
                )
        return result

    def _convert_tools(self, tool_defs: list[dict]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
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
            span.set_attribute(LLM_PROVIDER, "openai")
            span.set_attribute(LLM_MODEL, self.model)
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": self._convert_messages(messages),
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": stream,
            }
            if tools:
                kwargs["tools"] = self._convert_tools(tools)

            if not stream:
                response = await self.client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                if choice.message.content:
                    yield LLMChunk(
                        type=ChunkType.TEXT_DELTA, content=choice.message.content
                    )
                if choice.message.tool_calls:
                    for tc in choice.message.tool_calls:
                        yield LLMChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call=ToolCall(
                                id=tc.id,
                                name=tc.function.name,
                                arguments=json.loads(tc.function.arguments),
                            ),
                        )
                yield LLMChunk(type=ChunkType.DONE)
                return

            response = await self.client.chat.completions.create(**kwargs)
            current_tool_calls: dict[int, dict] = {}

            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                if delta.content:
                    yield LLMChunk(type=ChunkType.TEXT_DELTA, content=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        entry = current_tool_calls[idx]
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                entry["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                entry["arguments"] += tc_delta.function.arguments

                if chunk.choices[0].finish_reason:
                    for entry in current_tool_calls.values():
                        yield LLMChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call=ToolCall(
                                id=entry["id"],
                                name=entry["name"],
                                arguments=json.loads(entry["arguments"])
                                if entry["arguments"]
                                else {},
                            ),
                        )
                    yield LLMChunk(type=ChunkType.DONE)
                    return

    async def count_tokens(self, messages: list[Message]) -> int:
        try:
            enc = tiktoken.encoding_for_model(self.model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for msg in messages:
            total += 4  # message overhead
            if msg.content:
                total += len(enc.encode(msg.content))
        return total
